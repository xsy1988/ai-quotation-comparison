from .corrections import router as corrections_router
from .master import router as master_router
from .quotes import router as quotes_router
from .suggestions import router as suggestions_router
from .suppliers import router as suppliers_router
from .tasks import router as tasks_router

__all__ = [
    "tasks_router",
    "corrections_router",
    "suggestions_router",
    "master_router",
    "quotes_router",
    "suppliers_router",
]
