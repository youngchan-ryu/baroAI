// Dedicated loopback-only booth web entry point. No project Vite config or dotenv files.
import { closeSync, constants, fstatSync, lstatSync, openSync, readFileSync, realpathSync } from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath, URL } from 'node:url'
import react from '@vitejs/plugin-react'
import { build, createServer, preview } from 'vite'

const root = realpathSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'))
const cache = realpathSync(process.env.BARO_WEB_CACHE || '')
const port = Number(process.env.BARO_WEB_PORT)
const apiPort = Number(process.env.BARO_API_PORT)
const publicOrigin = process.env.BARO_PUBLIC_ORIGIN
const mode = process.env.BARO_WEB_MODE || 'demo'
const isPort = (value) => Number.isInteger(value) && value >= 1024 && value <= 65535
const relative = path.relative(root, cache)
if (!isPort(port) || !isPort(apiPort) || port === apiPort || relative === '' ||
    (!relative.startsWith('..' + path.sep) && relative !== '..') ||
    !['demo', 'service'].includes(mode) || (mode === 'service' && !publicOrigin)) {
  throw new Error('invalid_booth_web_configuration')
}

const publicRoot = path.join(root, 'public')
const assets = new Map([
  ['/icon.svg', 'image/svg+xml'],
  ['/icon-192.png', 'image/png'],
  ['/icon-512.png', 'image/png'],
  ['/manifest.webmanifest', 'application/manifest+json'],
  ...['gwanak', 'yeongeon', 'pyeongchang', 'siheung', 'suwon'].map(name => [`/maps/${name}.geojson`, 'application/geo+json']),
  ['/maps/NOTICE.txt', 'text/plain; charset=utf-8'],
])
function installAssetRoutes(vite) {
  vite.middlewares.use((req, res, next) => {
    let pathname
    try {
      pathname = new URL(req.url, 'http://127.0.0.1').pathname
    } catch {
      res.statusCode = 400
      return res.end()
    }
    const mime = assets.get(pathname)
    if (!mime) return next()
    if (req.method !== 'GET' && req.method !== 'HEAD') {
      res.statusCode = 405
      return res.end()
    }
    let fd
    try {
      const directory = lstatSync(publicRoot)
      if (!directory.isDirectory() || directory.isSymbolicLink() || realpathSync(publicRoot) !== publicRoot) {
        throw new Error('unsafe_asset_directory')
      }
      if (pathname.startsWith('/maps/')) {
        const maps = path.join(publicRoot, 'maps')
        const info = lstatSync(maps)
        if (!info.isDirectory() || info.isSymbolicLink() || realpathSync(maps) !== maps) {
          throw new Error('unsafe_asset_directory')
        }
      }
      fd = openSync(path.join(publicRoot, pathname.slice(1)), constants.O_RDONLY | constants.O_NOFOLLOW)
      const info = fstatSync(fd)
      if (!info.isFile() || info.nlink !== 1 || info.size > 2_000_000) throw new Error('unsafe_asset')
      const data = readFileSync(fd)
      res.setHeader('Content-Type', mime)
      res.setHeader('Content-Length', data.length)
      return res.end(req.method === 'HEAD' ? undefined : data)
    } catch {
      res.statusCode = 404
      return res.end()
    } finally {
      if (fd !== undefined) closeSync(fd)
    }
  })
}

const allowlistedAssets = {
  name: 'booth-allowlisted-assets',
  configureServer: installAssetRoutes,
  configurePreviewServer: installAssetRoutes,
}

function publicBoundary(host, buildRoot) {
  const loopbacks = ['localhost', '127.0.0.1', '[::1]']
  const allowedHosts = new Set([host, ...loopbacks.map(value => `${value}:${port}`)])
  if (mode === 'demo') loopbacks.forEach(value => allowedHosts.add(value))
  return {
    name: 'booth-public-boundary',
    configurePreviewServer(vite) {
      vite.middlewares.use((req, res, next) => {
        function end(status) {
          res.statusCode = status
          return res.end()
        }
        const hosts = req.headersDistinct.host ?? []
        if (hosts.length !== 1 || !allowedHosts.has(hosts[0])) return end(403)
        // Security markers must reach FastAPI even when a hop-by-hop header names them.
        const connection = (req.headers.connection ?? '').toLowerCase().split(',').map(value => value.trim())
        if (connection.some(value => ['host', 'origin', 'forwarded', 'cf-connecting-ip'].includes(value) ||
            value.startsWith('x-forwarded-'))) return end(400)
        if (/^\/api(?:\/|\?|$)/.test(req.url)) return next()
        const pathname = req.url.split('?')[0]
        if (assets.has(pathname)) return next()
        if (req.method !== 'GET' && req.method !== 'HEAD') return end(405)
        if (/^\/assets\/[a-zA-Z0-9_-]+-[a-zA-Z0-9_-]+\.(?:js|css)$/.test(pathname)) {
          const file = path.join(buildRoot, pathname.slice(1))
          if (!lstatSync(file, { throwIfNoEntry: false })?.isFile() || realpathSync(file) !== file) return end(404)
          return next()
        }
        if (pathname !== '/index.html' && (!/^(?:\/[a-zA-Z0-9_-]+)*\/?$/.test(pathname) ||
            /^\/(?:assets|maps|node_modules|public|scripts|src)(?:\/|$)/.test(pathname))) return end(404)
        const accept = req.headers.accept
        if (accept && !accept.includes('text/html') && !accept.includes('*/*')) return end(404)
        req.url = '/index.html'
        return next()
      })
    },
  }
}

const config = {
  root,
  configFile: false,
  envDir: false,
  envPrefix: [],
  publicDir: false,
  mode,
  cacheDir: cache,
  logLevel: 'silent',
  clearScreen: false,
  define: { 'import.meta.env.VITE_APP_MODE': JSON.stringify(mode) },
}
const listener = {
  host: '127.0.0.1', port, strictPort: true, open: false,
  proxy: { '^/api(?:/|\\?|$)': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false, xfwd: false, ws: false,
    rewrite: (requestPath) => requestPath.replace(/^\/api(?=\/)/, '').replace(/^\/api(?=\?|$)/, '/') } },
}

if (publicOrigin) {
  const origin = new URL(publicOrigin)
  if (origin.protocol !== 'https:' || origin.origin !== publicOrigin || !origin.hostname) {
    throw new Error('invalid_booth_public_origin')
  }
  // The bundle contains only compiled web assets. Maps and icons keep their explicit allowlist.
  const buildRoot = path.join(cache, 'dist')
  const buildOptions = { outDir: buildRoot, emptyOutDir: true, copyPublicDir: false, sourcemap: false }
  if (process.argv[2] === '--build') {
    await build({ ...config, build: buildOptions, plugins: [react()] })
  } else {
    await preview({
      ...config, build: buildOptions, plugins: [publicBoundary(origin.host, buildRoot), allowlistedAssets],
      preview: { ...listener, allowedHosts: true, cors: false },
    })
  }
} else {
  const server = await createServer({
    ...config,
    plugins: [allowlistedAssets, react()],
    server: {
      ...listener,
      fs: {
        strict: true,
        allow: [root, cache],
        deny: ['.env', '.env.*', '*.{crt,pem,key,p12,pfx,cer,der}', '.npmrc',
          '.yarnrc.yml', '**/.git/**', '**/public/**', '**/generation.json', '**/postgres-password', '**/redis.conf'],
      },
    },
  })
  await server.listen()
}
