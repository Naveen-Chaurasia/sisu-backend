import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api as v1_module
import apiv2 as v2_module


class CombinedApp:
    """Routes /v2/* to apiv2.app and everything else to api.app."""
    def __init__(self, app1, app2):
        self.app1 = app1
        self.app2 = app2

    async def __call__(self, scope, receive, send):
        if scope.get("path", "").startswith("/v2"):
            await self.app2(scope, receive, send)
        else:
            await self.app1(scope, receive, send)


combined = CombinedApp(v1_module.app, v2_module.app)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(combined, host="0.0.0.0", port=port)
