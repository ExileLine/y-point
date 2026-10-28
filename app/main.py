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
from app.core.middleware import ArtifactUploadLimitMiddleware, MyMiddleware
from app.core.exception_handlers import register_exception_handlers
from app.core.response import api_response
from app.api import api

project_config = get_config()


def create_app():
    """创建 FastAPI 应用并注册中间件、路由及静态资源。"""
    debug = project_config.DEBUG
    kw = {
        "debug": debug,
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
        **kw,
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
        mask_sensitive_headers=True,
    )

    app.add_middleware(ArtifactUploadLimitMiddleware)

    # 异常处理器注册
    register_exception_handlers(app, debug)

    # 路由注册
    app.include_router(api)

    # 静态资源(生产环境通过配置获取路径)
    static_dir = Path(__file__).resolve().parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/", summary="检查服务根入口", tags=["root"])
    async def root_redirect():
        """使用统一成功响应返回服务的基础存活状态。"""
        return api_response(
            data={
                "status": "ok",
            }
        )

    if project_config.DOCS_ENABLE:

        @app.get("/docs", summary="查看接口文档", include_in_schema=False)
        async def custom_swagger_ui_html():
            """返回供浏览器渲染的 Swagger HTML 页面，保留文档页面的媒体类型。"""
            return get_swagger_ui_html(
                openapi_url=app.openapi_url,
                title=f"{app.title} Swagger UI",
                swagger_js_url="/static/swagger-ui-bundle.js",
                swagger_css_url="/static/swagger-ui.css",
                swagger_favicon_url="/static/favicon.png",
            )

    return app


app = create_app()
