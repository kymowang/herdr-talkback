import asyncio
import subprocess
import sys


def test_plugin_imports_without_unix_modules():
    result = subprocess.run(
        [sys.executable, '-c', 'from herdr_talkback import plugin'],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


def test_service_lock_excludes_other_process_and_releases(tmp_path):
    from herdr_talkback.platform_support import service_lock

    path = tmp_path / 'service.lock'
    probe = (
        'from pathlib import Path; '
        'from herdr_talkback.platform_support import service_lock; '
        'import sys; '
        '\ntry:\n with service_lock(Path(sys.argv[1])): pass'
        '\nexcept BlockingIOError: sys.exit(7)'
    )
    with service_lock(path):
        assert subprocess.run([sys.executable, '-c', probe, str(path)]).returncode == 7
    assert subprocess.run([sys.executable, '-c', probe, str(path)]).returncode == 0


def test_local_transport_round_trip_and_cleanup(tmp_path):
    from herdr_talkback.transport import open_local_connection, start_local_server

    async def scenario():
        path = tmp_path / 'test.sock'

        async def echo(reader, writer):
            writer.write(await reader.readline())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await start_local_server(echo, path)
        async with server:
            reader, writer = await open_local_connection(path)
            writer.write('你好\n'.encode('utf-8'))
            await writer.drain()
            assert await reader.readline() == '你好\n'.encode('utf-8')
            writer.close()
            await writer.wait_closed()
        # A closed endpoint must be available for a fresh service instance.
        server = await start_local_server(echo, path)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
