'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const net = require('node:net');
const { spawn } = require('node:child_process');

async function atomicJSON(file, value) {
    const temporary = file + '.tmp';
    await fs.writeFile(temporary, JSON.stringify(value, null, 2), { mode: 0o600 });
    await fs.rename(temporary, file);
}
async function assertDirectory(root) {
    const resolved = path.resolve(root);
    await fs.mkdir(resolved, { recursive: true, mode: 0o700 });
    if ((await fs.realpath(resolved)) !== resolved || (await fs.lstat(resolved)).isSymbolicLink())
        throw new Error('The data directory must not be a symbolic link.');
    return resolved;
}
async function checkPort(host, port) {
    await new Promise((resolve, reject) => {
        const server = net.createServer();
        server.once('error', (error) =>
            reject(
                new Error(
                    error.code === 'EADDRINUSE'
                        ? `Port ${port} is already in use. Check existing services or choose another port.`
                        : 'Network address or port unavailable: ' + error.message,
                ),
            ),
        );
        server.listen({ host, port, exclusive: true }, () =>
            server.close((error) => (error ? reject(error) : resolve())),
        );
    });
}
function run(command, args, { signal, timeout = 600000, onLine = () => {}, spawnFn = spawn } = {}) {
    return new Promise((resolve, reject) => {
        if (signal?.aborted) {
            reject(new Error('Setup cancelled'));
            return;
        }
        let child,
            done = false,
            timedOut = false,
            stdout = '',
            stderr = '',
            buffer = '',
            killer;
        const finish = (error, result) => {
            if (done) return;
            done = true;
            clearTimeout(timer);
            clearTimeout(killer);
            signal?.removeEventListener('abort', abort);
            error ? reject(error) : resolve(result);
        };
        const kill = (sig) => {
            try {
                if (child && !done) process.kill(-child.pid, sig);
            } catch (error) {
                if (error.code !== 'ESRCH') child?.kill(sig);
            }
        };
        const abort = () => {
            kill('SIGTERM');
            killer = setTimeout(() => kill('SIGKILL'), 1500);
        };
        const timer = setTimeout(() => {
            timedOut = true;
            abort();
        }, timeout);
        signal?.addEventListener('abort', abort, { once: true });
        try {
            child = spawnFn(command, args, {
                stdio: ['ignore', 'pipe', 'pipe'],
                detached: true,
                windowsHide: true,
                env: {
                    ...process.env,
                    PYTHONUNBUFFERED: '1',
                    OPENBLAS_NUM_THREADS: '1',
                    OMP_NUM_THREADS: '2',
                },
            });
            child.on('error', (error) => finish(error));
            child.stdout.on('data', (chunk) => {
                const text = chunk.toString('utf8');
                stdout = (stdout + text).slice(-50000);
                buffer += text;
                let index;
                while ((index = buffer.indexOf('\n')) >= 0) {
                    const line = buffer.slice(0, index);
                    buffer = buffer.slice(index + 1);
                    try {
                        onLine(JSON.parse(line));
                    } catch {
                        /* third-party setup text is not a command */
                    }
                }
                if (buffer.length > 50000) buffer = buffer.slice(-50000);
            });
            child.stderr.on('data', (chunk) => {
                stderr = (stderr + chunk.toString('utf8')).slice(-8000);
            });
            child.on('close', (code) => {
                if (signal?.aborted) return finish(new Error('Setup cancelled'));
                if (timedOut)
                    return finish(new Error('Setup timed out. See logs/installation.log for details.'));
                if (code !== 0)
                    return finish(
                        new Error('Setup failed: ' + (stderr.trim() || stdout.trim()).slice(-2500)),
                    );
                try {
                    finish(null, JSON.parse(stdout.trim().split('\n').pop()));
                } catch {
                    finish(new Error('Setup did not return a valid response.'));
                }
            });
        } catch (error) {
            finish(error);
        }
    });
}
async function prepare(root, packageRoot, config, options = {}) {
    if (process.platform !== 'linux' || process.arch !== 'x64')
        throw new Error('This adapter version requires Linux with an x64 processor.');
    await assertDirectory(root);
    await checkPort(config.host, config.port);
    for (const name of ['logs', 'cache', 'voices']) await assertDirectory(path.join(root, name));
    const python = options.python || 'python3';
    const installed = await run(
        python,
        [
            '-u',
            path.join(packageRoot, 'worker', 'setup_runtime.py'),
            '--root',
            root,
            '--voice',
            config.speechProvider === 'speechify'
                ? 'de_DE-ramona-low'
                : config.speechProvider === 'sayit-google'
                  ? 'de_DE-thorsten-medium'
                  : config.piperVoice || 'de_DE-thorsten-medium',
        ],
        options,
    );
    if (
        !installed?.installed ||
        installed.root !== root ||
        installed.python !== path.join(root, 'venv', 'bin', 'python')
    )
        throw new Error('Runtime verification failed.');
    await atomicJSON(path.join(root, 'config.json'), config);
    return installed;
}
module.exports = { atomicJSON, assertDirectory, checkPort, run, prepare };
