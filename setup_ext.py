"""
Cython 编译脚本：把核心 .py 编译成 .so 以防反编译。

保留 .py 的文件（必须）：
  - run.py                        入口
  - app/__init__.py               包声明
  - app/main.py                   FastAPI 工厂（Pydantic + FastAPI 元类复杂，不 cythonize 更稳）
  - app/config.py                 pydantic-settings 元类风险
  - app/db.py                     SQLAlchemy 元类风险
  - app/models.py                 SQLAlchemy declarative 元类
  - app/deps.py                   FastAPI 依赖（装饰器）
  - app/routers/__init__.py       包声明
  - app/cli.py                    CLI 命令

编译的文件（核心业务）：
  - douyin_im.py                  抖音签名/加密/发消息核心
  - app/license.py                license 验证（最高优先级加密）
  - app/security.py               密码/JWT
  - app/crypto.py                 Fernet helper
  - app/csrf_mw.py                CSRF middleware
  - app/notify.py                 通知/邮件
  - app/scheduler.py              调度器
  - app/trigger.py                业务入口
  - app/storage.py                账户目录管理
  - app/templates_service.py      模板 DB 访问
  - app/routers/auth.py
  - app/routers/dashboard.py
  - app/routers/admin.py
  - app/routers/api.py
  - app/routers/login_flow.py

使用：
  python setup_ext.py build_ext --inplace
"""
from setuptools import setup
from Cython.Build import cythonize


TO_COMPILE = [
    "douyin_im.py",
    "app/license.py",
    "app/security.py",
    "app/crypto.py",
    "app/csrf_mw.py",
    "app/notify.py",
    "app/scheduler.py",
    "app/trigger.py",
    "app/storage.py",
    "app/templates_service.py",
    "app/routers/auth.py",
    "app/routers/dashboard.py",
    "app/routers/admin.py",
    "app/routers/api.py",
    "app/routers/login_flow.py",
]


setup(
    name="douyin_spark_app",
    ext_modules=cythonize(
        TO_COMPILE,
        compiler_directives={
            "language_level": 3,
            "always_allow_keywords": True,   # FastAPI 依赖注入要这个
            "emit_code_comments": False,
        },
        build_dir="build",
    ),
    zip_safe=False,
)
