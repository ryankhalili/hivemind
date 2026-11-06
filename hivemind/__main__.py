import os
import uvicorn

if __name__ == '__main__':
    uvicorn.run('hivemind.app:create_app',factory=True,host=os.environ.get('HIVE_BIND','127.0.0.1'),port=int(os.environ.get('HIVE_PORT','8787')),proxy_headers=True,forwarded_allow_ips='127.0.0.1')
