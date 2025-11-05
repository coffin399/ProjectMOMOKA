from __future__ import annotations

import os
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from modules import timer, initialize_util, initialize
from modules_forge.initialization import initialize_forge

# Initialize timer and core components
startup_timer = timer.startup_timer
startup_timer.record("launcher")

# Core initialization
initialize_forge()
initialize.imports()
initialize.check_versions()
initialize.initialize()

def _handle_exception(request: Request, e: Exception):
    error_info = vars(e)
    content = {
        "error": type(e).__name__,
        "detail": error_info.get("detail", ""),
        "body": error_info.get("body", ""),
        "message": str(e),
    }
    return JSONResponse(
        status_code=int(error_info.get("status_code", 500)), 
        content=jsonable_encoder(content)
    )

def create_api(app):
    from modules.api.api import Api
    from modules.call_queue import queue_lock
    return Api(app, queue_lock)

def api_only_worker():
    from fastapi import FastAPI
    from modules.shared_cmd_options import cmd_opts
    from modules import script_callbacks

    # Setup FastAPI app with middleware
    app = FastAPI(exception_handlers={Exception: _handle_exception})
    initialize_util.setup_middleware(app)
    
    # Initialize API
    api = create_api(app)
    
    # Call necessary callbacks
    script_callbacks.before_ui_callback()
    script_callbacks.app_started_callback(None, app)

    # Start the API server
    print(f"Startup time: {startup_timer.summary()}.")
    api.launch(
        server_name=initialize_util.gradio_server_name(),
        port=cmd_opts.port if cmd_opts.port else 7861,
        root_path=f"/{cmd_opts.subpath}" if cmd_opts.subpath else ""
    )

if __name__ == "__main__":
    from modules.shared_cmd_options import cmd_opts
    
    # Set default command line arguments for API mode
    cmd_opts.api = True
    cmd_opts.nowebui = True
    
    # Start the API server
    api_only_worker()

# This function is kept for backward compatibility
def api_only():
    Thread(target=api_only_worker, daemon=True).start()

if __name__ == "__main__":
    from modules.shared_cmd_options import cmd_opts
    
    # Force API mode
    cmd_opts.api = True
    cmd_opts.nowebui = True
    api_only_worker()

    main_thread.loop()
