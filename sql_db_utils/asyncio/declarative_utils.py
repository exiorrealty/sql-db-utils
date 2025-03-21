import importlib
import logging
import sys
import weakref
from typing import Annotated

from fastapi import Cookie, Query
from pydantic.alias_generators import to_pascal
from sqlalchemy import MetaData

from sql_db_utils.asyncio.declaratives import base_class_generator
from sql_db_utils.asyncio.session_management import SQLSessionManager
from sql_db_utils.config import ModuleConfig, PathConfig, PostgresConfig

declarative_utils = None


class DeclarativeUtils:
    """
    Utility class for generated declarative base classes.
    """

    async def __new__(
        cls, raw_database: str, tenant_id: str, session_manager: SQLSessionManager, schema: str, raw_db: bool = False
    ) -> None:
        obj = super().__new__(cls)
        obj.__init__(raw_database, tenant_id, session_manager, schema, raw_db)
        await obj._get_declarative_module()
        return obj

    def __init__(
        self, raw_database: str, tenant_id: str, session_manager: SQLSessionManager, schema: str, raw_db: bool = False
    ) -> None:
        self.raw_database: str = raw_database
        self.tenant_id: str = tenant_id
        self.session_manager: SQLSessionManager = session_manager
        self.raw_db = raw_db
        self.schema = schema
        self.declarative_module = None

    async def _pre_check(self):
        if self.declarative_module is None or isinstance(self.declarative_module, str):
            await self._get_declarative_module()

    async def _prepare_declarative_file(self, refresh: bool = False):
        declarative_tenant_directory = PathConfig.DECLARATIVES_PATH / self.tenant_id
        if not declarative_tenant_directory.exists():
            declarative_tenant_directory.mkdir(parents=True)
        tenant_init_file = declarative_tenant_directory / "__init__.py"
        if not tenant_init_file.exists():
            with open(tenant_init_file, "w") as f:
                f.write("")
        declarative_file = declarative_tenant_directory / f"async_{self.raw_database}_{self.schema}.py"
        if declarative_file.exists() and ModuleConfig.DEFER_GEN_REFRESH and not refresh:
            return f"{self.tenant_id}.async_{self.raw_database}_{self.schema}"
        try:
            logging.debug(f"Attempting to create declarative file: {declarative_file}")
            from sql_db_utils.asyncio.codegen import UTDeclarativeGenerator

            session = await self.session_manager.get_session(self.raw_database, None if self.raw_db else self.tenant_id)
            meta = MetaData()
            async with session.bind.begin() as conn:
                await conn.run_sync(meta.reflect, schema=self.schema)
            with open(declarative_file, "w", encoding="utf-8") as f:
                generator = UTDeclarativeGenerator(
                    raw_database=self.raw_database if self.raw_db else f"{self.tenant_id}__{self.raw_database}",
                    metadata=meta,
                    bind=session.bind,
                    options=set(),
                    schema=self.schema,
                )
                f.write(generator.generate())
        except ImportError:
            logging.debug(
                "Codegen Module not installed, if codegen is required please install using ut-sql-utils[codegen]"
            )
        except Exception as e:
            logging.error(f"Error creating declarative file: {e}")
            return False
        return f"{self.tenant_id}.async_{self.raw_database}_{self.schema}"

    async def _get_declarative_module(self):  # NOSONAR
        if declarative_module_path := await self._prepare_declarative_file():
            try:
                self.declarative_module = declarative_module_path
                declarative_module = importlib.import_module(
                    declarative_module_path, package=str(PathConfig.DECLARATIVES_PATH)
                )
                self.declarative_module = declarative_module
                return weakref.proxy(declarative_module)
            except Exception as e:
                if "No module named" in str(e):
                    try:
                        logging.debug("Module import failed due to module creation, trying to reload package")
                        try:
                            import asyncio

                            logging.warning("Emergency shutdown required - gracefully canceling tasks")
                            loop = asyncio.get_running_loop()
                            tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
                            logging.debug(f"Canceling {len(tasks)} pending tasks")

                            for task in tasks:
                                task.cancel()

                            # Wait for all tasks to complete with cancellation
                            if tasks:
                                await asyncio.gather(*tasks, return_exceptions=True)

                            logging.info("Tasks gracefully canceled, exiting")
                            sys.exit(1)
                        except ImportError:
                            logging.error("Not asyncio module, stopping using sys.exit")
                            sys.exit(1)
                    except Exception as e:
                        logging.debug(f"Module path: {declarative_module_path}")
                        logging.error(f"Error importing declarative module: {e}")
                        return None
                if "is already defined" in str(e):
                    try:
                        logging.debug("Module import failed due to conflict, trying to reload module")
                        await self.refresh_module(non_refresh=True)
                        return weakref.proxy(self.declarative_module)
                    except Exception as e:
                        logging.debug(f"Module path: {declarative_module_path}")
                        logging.error(f"Error importing declarative module: {e}")
                        return None
                logging.debug(f"Module path: {declarative_module_path}")
                logging.error(f"Error importing declarative module: {e}")
                return None

    def get_declarative_base(self):
        if self.declarative_module:
            return weakref.proxy(self.declarative_module.Base)
        return None

    def get_declarative_class(self, table_name: str):
        if self.declarative_module:
            try:
                pascal_table_name = to_pascal(table_name)
                return getattr(self.declarative_module, pascal_table_name)
            except AttributeError:
                try:
                    prefixed_table_name = f"t_{table_name}"
                    return getattr(self.declarative_module, prefixed_table_name)
                except AttributeError:
                    try:
                        return getattr(self.declarative_module, table_name)
                    except AttributeError:
                        try:
                            return getattr(self.declarative_module, table_name.replace("_", ""))
                        except AttributeError:
                            logging.error(f"Table {table_name} not found in declarative module")
                            return None
        return None

    async def refresh_module(self, non_refresh: bool = False):
        base_class_generator.remove_base_class(self.raw_database, self.schema)
        await self._prepare_declarative_file(True)
        if non_refresh:
            declarative_module = importlib.import_module(self.declarative_module)
        else:
            declarative_module = importlib.reload(self.declarative_module)
        self.declarative_module = declarative_module


class _DeclarativeUtilsFactory:
    """
    Factory class for declarative utilities.
    """

    def __init__(self) -> None:
        global declarative_utils
        declarative_utils = {}
        sys.path.append(str(PathConfig.DECLARATIVES_PATH))
        logging.debug(f"Added {PathConfig.DECLARATIVES_PATH} to sys.path")

    def get_declarative_utils_factory(
        self,
        raw_database: str,
        session_manager: SQLSessionManager,
    ):
        async def get_declarative_utils(
            tenant_id: Annotated[str, Cookie], schema: Annotated[str, Query] = PostgresConfig.PG_DEFAULT_SCHEMA
        ) -> DeclarativeUtils:
            global declarative_utils
            if declarative_util := declarative_utils.get(f"{raw_database}_{tenant_id}_{schema}"):
                await declarative_util._pre_check()
                return declarative_util
            else:
                declarative_util = await DeclarativeUtils(raw_database, tenant_id, session_manager, schema)
                declarative_utils[f"{raw_database}_{tenant_id}_{schema}"] = declarative_util
                return declarative_util

        return get_declarative_utils

    def get_schema_mandated_declarative_utils_factory(
        self,
        raw_database: str,
        session_manager: SQLSessionManager,
        schema: str,
    ):
        async def get_declarative_utils(tenant_id: Annotated[str, Cookie]) -> DeclarativeUtils:
            global declarative_utils
            if declarative_util := declarative_utils.get(f"{raw_database}_{tenant_id}_{schema}"):
                await declarative_util._pre_check()
                return declarative_util
            else:
                declarative_util = await DeclarativeUtils(raw_database, tenant_id, session_manager, schema)
                declarative_utils[f"{raw_database}_{tenant_id}_{schema}"] = declarative_util
                return declarative_util

        return get_declarative_utils

    async def get_declarative_utils(
        self,
        raw_database: str,
        tenant_id: str,
        session_manager: SQLSessionManager,
        schema: str = PostgresConfig.PG_DEFAULT_SCHEMA,
        raw_db: bool = False,
    ) -> DeclarativeUtils:
        global declarative_utils
        if declarative_util := declarative_utils.get(f"{raw_database}_{tenant_id}_{schema}"):
            await declarative_util._pre_check()
            return declarative_util
        else:
            declarative_util = await DeclarativeUtils(raw_database, tenant_id, session_manager, schema, raw_db)
            declarative_utils[f"{raw_database}_{tenant_id}_{schema}"] = declarative_util
            return declarative_util


DeclarativeUtilsFactory = _DeclarativeUtilsFactory()

__all__ = ["DeclarativeUtilsFactory", "DeclarativeUtils"]
