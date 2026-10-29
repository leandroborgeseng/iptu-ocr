import os
from waitress import serve
from . import create_app

# One process only: the persistent SQLite queue has one owning worker thread.
if __name__ == '__main__':
    serve(create_app(), host='0.0.0.0', port=int(os.environ.get('PORT', '8000')), threads=8)
