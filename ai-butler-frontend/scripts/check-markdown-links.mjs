import { existsSync, readFileSync, readdirSync } from 'node:fs'
import { dirname, extname, join, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const documents = []

function visit(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    if (['.git', 'node_modules', 'dist', 'coverage'].includes(entry.name)) continue
    const path = join(directory, entry.name)
    if (entry.isDirectory()) visit(path)
    else if (extname(entry.name) === '.md') documents.push(path)
  }
}

visit(root)
const failures = []
const link = /\[[^\]]*\]\(([^)]+)\)/g
for (const document of documents) {
  for (const match of readFileSync(document, 'utf8').matchAll(link)) {
    const target = match[1]
    if (!target || /^(https?:|mailto:|#)/.test(target)) continue
    const local = decodeURIComponent(target.split('#')[0])
    if (local && !existsSync(resolve(dirname(document), local))) {
      failures.push(`${relative(root, document)} -> ${target}`)
    }
  }
}
if (failures.length) {
  console.error(`Broken Markdown links:\n- ${failures.join('\n- ')}`)
  process.exit(1)
}
console.log('Markdown links are valid')
