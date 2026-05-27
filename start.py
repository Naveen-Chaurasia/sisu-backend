import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api as v1_module
import apiv2 as v2_module
from mines.api_mines import app as mines_app


class CombinedApp:
    """Routes /v2/* to apiv2.app, /mines/* to mines_app, else to api.app."""
    def __init__(self, app1, app2, app3):
        self.app1 = app1
        self.app2 = app2
        self.app3 = app3

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if path.startswith("/v2"):
            await self.app2(scope, receive, send)
        elif path.startswith("/mines"):
            await self.app3(scope, receive, send)
        else:
            await self.app1(scope, receive, send)


combined = CombinedApp(v1_module.app, v2_module.app, mines_app)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(combined, host="0.0.0.0", port=port)
