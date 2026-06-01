import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api as v1_module
import apiv2 as v2_module
from mines4.api_m4 import app as mines4_app


class CombinedApp:
    """
    Routes:
      /v2/*     → apiv2.app
      /mines4/* → api_m4.app  (path rewritten: /mines4/x → /x)
      else      → api.app
    """
    def __init__(self, app1, app2, app3):
        self.app1 = app1   # api (v1)
        self.app2 = app2   # apiv2
        self.app3 = app3   # mines4

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if path.startswith("/v2"):
            await self.app2(scope, receive, send)
        elif path.startswith("/mines4"):
            new_path = path[7:]  # drop the 7-char "/mines4" prefix
            scope = dict(scope)
            scope["path"]     = new_path
            scope["raw_path"] = new_path.encode()
            await self.app3(scope, receive, send)
        else:
            await self.app1(scope, receive, send)


combined = CombinedApp(v1_module.app, v2_module.app, mines4_app)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(combined, host="0.0.0.0", port=port)
