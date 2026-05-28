import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import api as v1_module
import apiv2 as v2_module
from mines.api_mines import app as mines_app
from mines.api_mine_supabase import app as mines2_app


class CombinedApp:
    """
    Routes:
      /v2/*    → apiv2.app
      /mines2/* → api_mine_supabase.app  (path rewritten: /mines2/x → /mines/x)
      /mines/* → api_mines.app
      else     → api.app
    """
    def __init__(self, app1, app2, app3, app4):
        self.app1 = app1   # api (v1)
        self.app2 = app2   # apiv2
        self.app3 = app3   # mines  (hardcoded)
        self.app4 = app4   # mines2 (supabase)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if path.startswith("/v2"):
            await self.app2(scope, receive, send)
        elif path.startswith("/mines2"):
            # Strip /mines2 prefix: /mines2/mines/list → /mines/list
            new_path = path[7:]  # drop the 7-char "/mines2" prefix
            scope = dict(scope)
            scope["path"]     = new_path
            scope["raw_path"] = new_path.encode()
            await self.app4(scope, receive, send)
        elif path.startswith("/mines"):
            await self.app3(scope, receive, send)
        else:
            await self.app1(scope, receive, send)


combined = CombinedApp(v1_module.app, v2_module.app, mines_app, mines2_app)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(combined, host="0.0.0.0", port=port)
