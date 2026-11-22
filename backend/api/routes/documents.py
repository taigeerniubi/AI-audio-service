"""定义资料上传、删除、索引重建和知识检索接口。"""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile

from backend.api.dependencies import Runtime, get_runtime
from backend.schemas.tools import SearchArgs
from backend.services.knowledge.ingestion import MAX_FILE_BYTES, SUPPORTED

router = APIRouter(prefix="/api/products/{product_id}", tags=["documents"])


@router.get("/documents")
def documents(product_id: str, runtime: Runtime = Depends(get_runtime)):
    """列出资料的公开索引状态。

    Args:
        product_id: 目标产品的唯一 ID。
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        资料公开概要，不包含完整原文和向量。
    """
    return runtime.knowledge.documents(product_id)


@router.post("/documents", status_code=201)
async def upload_document(
    product_id: str, file: UploadFile, runtime: Runtime = Depends(get_runtime),
    upload_id: str | None = Query(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
):
    """限制文件格式和读取量后交给资料业务服务。

    Args:
        product_id: 目标产品的唯一 ID。
        file: 浏览器上传的文件，读取结束后始终关闭。
        runtime: 当前应用独享的配置、存储与业务服务容器。
        upload_id: 可选上传关联 ID，用于页面查询本次操作状态。

    Returns:
        成功入库的资料公开概要。
    """
    try:
        runtime.products.require(product_id)
        name = (file.filename or "").replace("\\", "/").split("/")[-1]
        if Path(name).suffix.lower() not in SUPPORTED:
            raise HTTPException(415, "支持 TXT、MD、PDF、DOCX、XLSX")
        data = await file.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise HTTPException(413, "单份资料最多 10 MB")
    finally:
        await file.close()
    return await runtime.knowledge.upload(product_id, name, data, upload_id)


@router.get("/document-processing")
def document_processing(product_id: str, runtime: Runtime = Depends(get_runtime)):
    """读取资料处理的真实阶段，供页面轮询及刷新后恢复显示。

    Args:
        product_id: 目标产品的唯一 ID。
        runtime: 当前应用独享的依赖容器。

    Returns:
        最近处理状态；本进程尚无记录时为空。
    """
    return runtime.knowledge.processing_status(product_id)


@router.post("/documents/{document_id}/index")
async def reindex_document(
    product_id: str, document_id: str, runtime: Runtime = Depends(get_runtime),
    upload_id: str | None = Query(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
):
    """重新生成已有文档的向量索引。

    Args:
        product_id: 目标产品的唯一 ID。
        document_id: 目标资料的唯一 ID。
        runtime: 当前应用独享的配置、存储与业务服务容器。
        upload_id: 可选索引操作关联 ID。

    Returns:
        已更新索引的资料概要。
    """
    return await runtime.knowledge.reindex(product_id, document_id, upload_id)


@router.post("/search")
async def search_documents(product_id: str, body: SearchArgs, runtime: Runtime = Depends(get_runtime)):
    """校验检索词并返回混合检索结果。

    Args:
        product_id: 目标产品的唯一 ID。
        body: 已验证的检索请求，包含 query 字段。
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        证据片段、用量及 RRF 配置。
    """
    return await runtime.knowledge.search(product_id, body.query)


@router.delete("/documents/{document_id}")
def delete_document(product_id: str, document_id: str, runtime: Runtime = Depends(get_runtime)):
    """删除指定产品下的资料与索引。

    Args:
        product_id: 目标产品的唯一 ID。
        document_id: 目标资料的唯一 ID。
        runtime: 当前应用独享的配置、存储与业务服务容器。

    Returns:
        删除成功标记。
    """
    return runtime.knowledge.delete(product_id, document_id)
