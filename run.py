import os
from app import create_app

app = create_app(os.environ.get('APP_ENV', 'development'))

if __name__ == '__main__':
    app.run()
