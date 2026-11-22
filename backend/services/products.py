"""处理产品档案的创建、更新与存在性校验。"""

from backend.core.errors import ServiceError
from backend.db.store import Store


class ProductService:
    """在持久化之前执行产品相关业务约束。"""

    def __init__(self, store: Store):
        """绑定当前应用的存储实例。

        Args:
            store: 当前应用的数据库存储服务。
        """
        self.store = store

    def require(self, product_id: str) -> dict:
        """返回产品；不存在时产生可供路由转换的 404 错误。

        Args:
            product_id: 目标产品的唯一 ID。

        Returns:
            已存在的产品档案。
        """
        product = self.store.product(product_id)
        if not product:
            raise ServiceError(404, "产品不存在")
        return product

    def save(self, body: dict, product_id: str | None = None) -> dict:
        """创建产品或校验更新目标后保存新版本。

        Args:
            body: 经 schema 验证的产品字段。
            product_id: 更新目标；为空时创建产品。

        Returns:
            保存后的产品 ID、版本及档案。
        """
        if product_id is not None:
            self.require(product_id)
        return self.store.save_product(body, product_id)
