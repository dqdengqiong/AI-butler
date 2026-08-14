import { createHash } from 'node:crypto'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const backend = resolve(root, process.env.BACKEND_REPOSITORY ?? '../ai-butler-backend')
const source = resolve(process.env.BACKEND_OPENAPI_PATH ?? join(backend, 'openapi.json'))
const generated = join(root, 'src/api/generated/schema.d.ts')
const lockPath = join(root, 'src/api/generated/contract-lock.json')
const check = process.argv.includes('--check')
const temporary = check ? mkdtempSync(join(tmpdir(), 'ai-butler-openapi-')) : null
const output = temporary ? join(temporary, 'schema.d.ts') : generated

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { cwd: root, encoding: 'utf8', ...options })
  if (result.status !== 0) {
    process.stderr.write(result.stderr || result.stdout)
    process.exit(result.status ?? 1)
  }
  return result.stdout.trim()
}

const openapiContent = readFileSync(source, 'utf8')
const openapi = JSON.parse(openapiContent)
run(join(root, 'node_modules/.bin/openapi-typescript'), [source, '--output', output])

const backendCommit = run('git', ['-C', backend, 'rev-parse', 'HEAD'])
const generator = JSON.parse(
  readFileSync(join(root, 'node_modules/openapi-typescript/package.json'), 'utf8'),
).version
const lock = `${JSON.stringify(
  {
    apiTag: `api-v${openapi.info.version}`,
    backendCommit,
    generator: `openapi-typescript@${generator}`,
    schemaSha256: createHash('sha256').update(openapiContent).digest('hex'),
  },
  null,
  2,
)}\n`

if (check) {
  const schemaMatches = readFileSync(output, 'utf8') === readFileSync(generated, 'utf8')
  const lockMatches = lock === readFileSync(lockPath, 'utf8')
  rmSync(temporary, { recursive: true, force: true })
  if (!schemaMatches || !lockMatches) {
    console.error('OpenAPI generated files are stale; run pnpm api:sync')
    process.exit(1)
  }
  console.log('OpenAPI generated files are current')
} else {
  writeFileSync(lockPath, lock, 'utf8')
  console.log(`synchronized ${openapi.info.title} ${openapi.info.version}`)
}
