"""定义产品档案的查询、创建和更新接口。"""

from fastapi import APIRouter, Depends

from backend.api.dependencies import Runtime, get_runtime
from backend.schemas.products import ProductInput

router = APIRouter(prefix="/api/products", tags=["products"])


@router.get("")
def products(runtime: Runtime = Depends(get_runtime)):
    """列出可供演示选择的产品。

    Args:
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        产品档案列表。
    """
    return runtime.store.products()


@router.post("", status_code=201)
def create_product(body: ProductInput, runtime: Runtime = Depends(get_runtime)):
    """校验产品请求并创建初始版本。

    Args:
        body: 经参数模型验证的产品档案。
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        含 ID 和版本号的新产品。
    """
    return runtime.products.save(body.model_dump())


@router.put("/{product_id}")
def update_product(product_id: str, body: ProductInput, runtime: Runtime = Depends(get_runtime)):
    """保存指定产品的新版本。

    Args:
        product_id: 目标产品的唯一 ID。
        body: 经参数模型验证的产品档案。
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        保存后的产品及新版本号。
    """
    return runtime.products.save(body.model_dump(), product_id)
