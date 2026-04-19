"""
存储路径辅助 + 账户上下文操作（桥接 douyin_im 模块）。
"""
import os
import shutil
from .config import settings

# 确保 douyin_im 拿到正确的 DOUYIN_DATA_ROOT
os.environ["DOUYIN_DATA_ROOT"] = settings.data_dir

# 导入后才能触发 _ACCOUNTS_ROOT 读取 env
import douyin_im as dy                                            # noqa: E402

# 重新 export（方便调用方）
AccountCtx = dy.AccountCtx
set_account_ctx = dy.set_account_ctx
get_account_ctx = dy.get_account_ctx


def account_dir(user_id: int, account_id: int) -> str:
    """获取某用户某账户的目录（不改变线程 ctx）"""
    return AccountCtx(user_id=user_id, account_id=account_id).dir()


def delete_account_dir(user_id: int, account_id: int):
    """彻底删除某账户数据目录"""
    d = account_dir(user_id, account_id)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


def safe_under_data_dir(path: str) -> bool:
    """防越界：确保 path 在 DATA_DIR 下"""
    root = os.path.realpath(settings.data_dir)
    target = os.path.realpath(path)
    try:
        return os.path.commonpath([root, target]) == root
    except ValueError:
        return False
