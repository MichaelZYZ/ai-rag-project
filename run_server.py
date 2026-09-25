"""Start the demo on an available local port."""

import errno
import socket

import uvicorn


def available_port(start=8000, stop=8010):
    for port in range(start, stop + 1):
        with socket.socket() as probe:
            try:
                probe.bind(('127.0.0.1', port))
            except OSError as exc:
                if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                    continue
                raise
            return port
    raise RuntimeError(f'端口 {start}–{stop} 均被占用，请关闭旧服务后重试。')


if __name__ == '__main__':
    port = available_port()
    if port != 8000:
        print(f'端口 8000 已被占用，旧页面可能仍在运行。请打开下面的新地址：', flush=True)
    print(f'\n产品知识问答演示：http://127.0.0.1:{port}/\n', flush=True)
    uvicorn.run('app.main:app', host='127.0.0.1', port=port)
