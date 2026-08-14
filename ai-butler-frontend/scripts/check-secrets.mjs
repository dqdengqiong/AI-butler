import { readFileSync, readdirSync } from 'node:fs'
import { extname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('..', import.meta.url)))
const ignored = new Set(['.git', 'node_modules', 'dist', 'coverage', 'pnpm-lock.yaml'])
const textExtensions = new Set(['.js', '.json', '.md', '.mjs', '.ts', '.vue', '.yaml', '.yml'])
const patterns = [
  /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/,
  /(?:api[_-]?key|secret|token)\s*[:=]\s*['"][A-Za-z0-9_-]{24,}['"]/i,
]
const findings = []

function visit(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (ignored.has(entry.name) || entry.name.startsWith('.env')) continue
    const path = join(directory, entry.name)
    if (entry.isDirectory()) visit(path)
    else if (textExtensions.has(extname(entry.name))) {
      const content = readFileSync(path, 'utf8')
      if (patterns.some((pattern) => pattern.test(content))) findings.push(relative(root, path))
    }
  }
}

visit(root)
if (findings.length) {
  console.error(`Potential secrets found:\n- ${findings.join('\n- ')}`)
  process.exit(1)
}
console.log('No obvious secrets found')
