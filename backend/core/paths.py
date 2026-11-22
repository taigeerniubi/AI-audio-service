"""提供与工作目录无关的项目资源路径。"""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
INGESTION_TEMP = PROJECT_ROOT / ".cache" / "ingestion"
