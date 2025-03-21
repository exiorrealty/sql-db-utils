import logging
from typing import Annotated, Callable, List, Union

from sqlalchemy import Engine, MetaData, NullPool, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy_utils import create_database, database_exists

from sql_db_utils.config import ModuleConfig, PostgresConfig
from sql_db_utils.declaratives import DeclarativeBaseClassFactory
from sql_db_utils.sql_creations import create_default_psql_dependencies
from sql_db_utils.sql_retry_handler import RetryingQuery


class SQLSessionManager:
    __slots__ = ("_db_engines", "database_uri", "_postcreate_auto", "_postcreate_manual")

    def __init__(self, database_uri: Union[str, None] = None) -> None:
        self._db_engines = {}
        self.database_uri = database_uri or PostgresConfig.POSTGRES_URI
        self._postcreate_auto: dict = {}
        self._postcreate_manual: dict = {}

    def __del__(self) -> None:
        for engine in self._db_engines.values():
            engine.dispose()

    def _get_fully_qualified_db(self, database: str, tenant_id: Union[str, None] = None) -> str:
        return f"{tenant_id}__{database}" if tenant_id else database

    def _ensure_engine_connection(self, _engine_obj: Engine):
        for _ in range(PostgresConfig.PG_MAX_RETRY):
            try:
                if not database_exists(_engine_obj.url):
                    create_database(_engine_obj.url)
                with _engine_obj.connect() as conn:
                    conn.execute(text("SELECT 1"))
                    break
            except OperationalError as oe:
                if "server login has been failing" not in str(oe):
                    logging.info(f"Server connection failed, retry {_}")
                    continue
                logging.error("Server connection failed")

    def _get_engine(
        self, database: str, tenant_id: Union[str, None] = None, metadata: Union[MetaData, None] = None
    ) -> Engine:
        qualified_db_name = self._get_fully_qualified_db(database=database, tenant_id=tenant_id)
        if not (engine := self._db_engines.get(qualified_db_name)):
            logging.debug(f"Creating engine for database: {qualified_db_name}")
            if PostgresConfig.PG_ENABLE_POOLING:
                engine = create_engine(
                    f"{self.database_uri}/{qualified_db_name}?application_name={ModuleConfig.MODULE_NAME}",
                    connect_args=(
                        {
                            "connect_timeout": PostgresConfig.PG_CONNECTION_TIMEOUT,
                        }
                        | PostgresConfig.PG_CONNECT_ARGS
                    ),
                    pool_size=PostgresConfig.PG_MIN_CONNECTION,
                    max_overflow=PostgresConfig.PG_MAX_CONNECTION,
                    pool_pre_ping=True,
                    pool_use_lifo=True,
                    future=True,
                    pool_recycle=PostgresConfig.PG_POOL_RECYCLE,
                    isolation_level="AUTOCOMMIT",
                )
            else:
                engine = create_engine(
                    f"{self.database_uri}/{qualified_db_name}?application_name={ModuleConfig.MODULE_NAME}",
                    connect_args=(
                        {
                            "connect_timeout": PostgresConfig.PG_CONNECTION_TIMEOUT,
                        }
                        | PostgresConfig.PG_CONNECT_ARGS
                    ),
                    poolclass=NullPool,
                    future=True,
                    isolation_level="AUTOCOMMIT",
                )
            self._ensure_engine_connection(engine)
            if not PostgresConfig.PG_ANTI_PERSISTENT:
                self._db_engines[qualified_db_name] = engine
            create_default_psql_dependencies(
                metadata=metadata or DeclarativeBaseClassFactory(database).metadata, engine_obj=engine
            )
            self.run_postcreate(engine, database, tenant_id)
        return engine

    def get_session(
        self,
        database: str,
        tenant_id: Union[str, None] = None,
        metadata: Union[MetaData, None] = None,
        retrying: bool = False,
    ) -> Session:
        if PostgresConfig.PG_RETRY_QUERY or retrying:
            return Session(
                bind=self._get_engine(database=database, tenant_id=tenant_id, metadata=metadata),
                future=True,
                query_cls=RetryingQuery,
            )
        return Session(
            bind=self._get_engine(database=database, tenant_id=tenant_id, metadata=metadata),
            future=True,
            expire_on_commit=False,
        )

    def get_engine_obj(
        self, database: str, tenant_id: Union[str, None] = None, metadata: Union[MetaData, None] = None
    ) -> Engine:
        return self._get_engine(database=database, tenant_id=tenant_id, metadata=metadata)

    def get_db_factory(self, database: str, retrying: bool = False) -> Callable:
        from fastapi import Cookie

        def get_db(tenant_id: Annotated[str, Cookie]):
            yield self.get_session(database=database, tenant_id=tenant_id, retrying=retrying)

        return get_db

    def postcreate_decorator(self, raw_db: str | List[str], postcreate_store: str) -> Callable:
        postcreate_store = getattr(self, postcreate_store)

        def decorator(func: Callable) -> None:
            if isinstance(raw_db, list):
                for db in raw_db:
                    postcreate_auto = postcreate_store.get(db, [])
                    postcreate_auto.append(func)
                    postcreate_store[db] = postcreate_auto
            else:
                postcreate_auto = postcreate_store.get(raw_db, [])
                postcreate_auto.append(func)
                postcreate_store[raw_db] = postcreate_auto

        return decorator

    def register_postcreate(self, raw_db: str | List[str]) -> Callable:
        return self.postcreate_decorator(raw_db, "_postcreate_auto")

    def register_postcreate_manual(self, raw_db: str | List[str]) -> Callable:
        return self.postcreate_decorator(raw_db, "_postcreate_manual")

    def run_postcreate(self, engine: Engine, raw_db: str, tenant_id: Union[str, None] = None) -> None:
        session = Session(bind=engine, future=True, expire_on_commit=False)
        with session.begin():
            for postcreate_func in self._postcreate_auto.get(raw_db, []):
                result = postcreate_func(tenant_id)
                if isinstance(result, list):
                    for statement in result:
                        session.execute(statement)
                else:
                    session.execute(result)
        for postcreate_func in self._postcreate_manual.get(raw_db, []):
            postcreate_func(session, tenant_id)
        session.commit()
        session.close()
        logging.info(f"Postcreate for {raw_db} completed")
