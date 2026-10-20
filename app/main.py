# -*- coding: utf-8 -*-
# @Time    : 2026-10-20 14:55:26
# @Author  : yangyuexiong
# @File    : main.py

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html

from app.core.config import get_config
from app.core.lifespan import lifespan
from app.core.middleware import MyMiddleware
from app.core.exception_handlers import register_exception_handlers
from app.api import api

project_config = get_config()


def create_app():
    debug = project_config.DEBUG
    kw = {
        "debug": debug
    }

    app = FastAPI(
        title=project_config.DOCS_TITLE,
        description=project_config.DOCS_DESCRIPTION,
        summary=project_config.DOCS_SUMMARY,
        version=project_config.DOCS_VERSION,
        openapi_url=project_config.DOCS_OPENAPI_URL if project_config.DOCS_ENABLE else None,
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,  # 事件注册(应用启动前与关闭后执行的事件处理器)
        **kw
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=project_config.ALLOW_ORIGINS,
        allow_credentials=project_config.ALLOW_CREDENTIALS,
        allow_methods=project_config.ALLOW_METHODS,
        allow_headers=project_config.ALLOW_HEADERS,
    )

    app.add_middleware(
        MyMiddleware,
        log_headers=debug,
        log_body=debug,
        exclude_paths=["/docs", "/openapi.json", "/redoc", "/static*"],
        sensitive_headers=project_config.SENSITIVE_HEADERS,
        mask_sensitive_headers=project_config.MASK_SENSITIVE_HEADERS,
    )

    # 异常处理器注册
    register_exception_handlers(app, debug)

    # 路由注册
    app.include_router(api)

    # 静态资源(生产环境通过配置获取路径)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", summary="root", tags=["root"])
    async def root_redirect():
        """root"""
        return {
            "status": "ok",
        }

    if project_config.DOCS_ENABLE:
        @app.get(f"/docs", include_in_schema=False)
        async def custom_swagger_ui_html():
            return get_swagger_ui_html(
                openapi_url=app.openapi_url,
                title=f"{app.title} Swagger UI",
                swagger_js_url=f"/static/swagger-ui-bundle.js",
                swagger_css_url=f"/static/swagger-ui.css",
                swagger_favicon_url=f"/static/favicon.png",
            )
    return app


app = create_app()
