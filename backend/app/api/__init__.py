from .corrections import router as corrections_router
from .master import router as master_router
from .suggestions import router as suggestions_router
from .tasks import router as tasks_router

__all__ = ["tasks_router", "corrections_router", "suggestions_router", "master_router"]
