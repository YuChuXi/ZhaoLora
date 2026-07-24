"""Pydantic 模型包（按领域拆分，对外仍从 ``services.models`` 导入）。

引用: 各 routes / services。
"""

from services.models.dataset import (
    ScannedImage,
    WorkDirInfo,
    TxtDirInfo,
    AnnotatorState,
    ImageListItem,
    SimilarItem,
    SimilarResponse,
    DirsResponse,
)
from services.models.tasks import (
    TaskEntry,
    TaskListFile,
    TaskInfo,
    TaskCard,
    TaskListResponse,
    TaskCandidatesResponse,
    TaskOkResponse,
    DatasetItem,
    DatasetsResponse,
)
from services.models.booru import (
    BooruPost,
    BooruSearchResult,
    BooruSourcesResponse,
    BooruDownloadResult,
    BooruUndownloadResult,
)
from services.models.dedupe import (
    DedupeMember,
    DedupeGroup,
    DedupeScanResult,
    DedupeMoveResponse,
)
from services.models.ready import (
    MaskStats,
    ReadyDirsResponse,
    ReadySubset,
    ReadyTomlResult,
)
from services.models.tags import (
    TagStatRow,
    TagStatsResult,
    TagCleanSample,
    TagCleanResult,
)
from services.models.jobs import (
    AestheticSummary,
    JobSubmitResponse,
    JobResult,
    JobSnapshot,
    JobCancelResponse,
)
from services.models.crop import (
    CropMatchConfig,
    CropImageItem,
    CropPageConfig,
    CropSavedItem,
    CropSaveResponse,
)
from services.models.annotate import (
    ToggleResponse,
    SelectAllResponse,
    CountResponse,
    AnnotatorStateResponse,
)
from services.models.docs import (
    DocNavItem,
    DocsListResponse,
    DocContentResponse,
)
from services.models.paint import (
    PaintSaveResponse,
    DefaultAlphaResponse,
    PaintImageItem,
)
from services.models.pages import (
    AestheticDirsResponse,
    AestheticStartResponse,
    TaggingDirsResponse,
    TaggingStartResponse,
)

__all__ = [
    'ScannedImage',
    'WorkDirInfo',
    'TxtDirInfo',
    'AnnotatorState',
    'ImageListItem',
    'SimilarItem',
    'SimilarResponse',
    'DirsResponse',
    'TaskEntry',
    'TaskListFile',
    'TaskInfo',
    'TaskCard',
    'TaskListResponse',
    'TaskCandidatesResponse',
    'TaskOkResponse',
    'DatasetItem',
    'DatasetsResponse',
    'BooruPost',
    'BooruSearchResult',
    'BooruSourcesResponse',
    'BooruDownloadResult',
    'BooruUndownloadResult',
    'DedupeMember',
    'DedupeGroup',
    'DedupeScanResult',
    'DedupeMoveResponse',
    'MaskStats',
    'ReadyDirsResponse',
    'ReadySubset',
    'ReadyTomlResult',
    'TagStatRow',
    'TagStatsResult',
    'TagCleanSample',
    'TagCleanResult',
    'AestheticSummary',
    'JobSubmitResponse',
    'JobResult',
    'JobSnapshot',
    'JobCancelResponse',
    'CropMatchConfig',
    'CropImageItem',
    'CropPageConfig',
    'CropSavedItem',
    'CropSaveResponse',
    'ToggleResponse',
    'SelectAllResponse',
    'CountResponse',
    'AnnotatorStateResponse',
    'DocNavItem',
    'DocsListResponse',
    'DocContentResponse',
    'PaintSaveResponse',
    'DefaultAlphaResponse',
    'PaintImageItem',
    'AestheticDirsResponse',
    'AestheticStartResponse',
    'TaggingDirsResponse',
    'TaggingStartResponse',
]
