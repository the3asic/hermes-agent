import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { createRequire } from 'node:module'

const require = createRequire(path.join(process.cwd(), 'package.json'))
const ts = require('typescript')
const root = process.cwd()
const i18n = 'apps/desktop/src/i18n'
const written = []

function parsed(relative) {
  const source = fs.readFileSync(path.join(root, relative), 'utf8')
  return { source, tree: ts.createSourceFile(relative, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS) }
}

function write(relative, source) {
  fs.writeFileSync(path.join(root, relative), source)
  written.push(relative)
}

function replaceRange(source, node, replacement, tree) {
  return source.slice(0, node.getStart(tree)) + replacement + source.slice(node.getEnd())
}

for (const locale of ['en', 'ja', 'zh']) {
  const relative = `${i18n}/${locale}.ts`
  const { source, tree } = parsed(relative)
  const declaration = tree.statements.filter(ts.isVariableStatement).flatMap(statement => [...statement.declarationList.declarations])
    .find(node => node.name.getText(tree) === locale)
  if (!declaration || !ts.isObjectLiteralExpression(declaration.initializer)) throw new Error(`Inspect ${relative}`)
  const property = declaration.initializer.properties.find(node => ts.isPropertyAssignment(node) && node.name.getText(tree) === 'cron')
  if (!property || !ts.isObjectLiteralExpression(property.initializer)) throw new Error(`Inspect cron locale ${locale}`)
  const name = `${locale}Cron`
  const sibling = `${i18n}/${locale}_cron.ts`
  if (fs.existsSync(path.join(root, sibling))) throw new Error(`Preserve ${sibling}`)
  write(sibling, `import type { CronTranslations } from './types_cron'\n\nexport const ${name}: CronTranslations = ${property.initializer.getText(tree)}\n`)
  write(relative, `import { ${name} } from './${locale}_cron'\n` + replaceRange(source, property, `cron: ${name}`, tree))
}

{
  const relative = `${i18n}/types.ts`
  const { source, tree } = parsed(relative)
  const declaration = tree.statements.find(node => ts.isInterfaceDeclaration(node) && node.name.text === 'Translations')
  if (!declaration) throw new Error('Inspect the translation contract')
  const property = declaration.members.find(node => ts.isPropertySignature(node) && node.name.getText(tree) === 'cron')
  if (!property || !ts.isTypeLiteralNode(property.type)) throw new Error('Inspect cron translation types')
  const references = new Set()
  function collect(node) {
    if (ts.isTypeReferenceNode(node)) references.add(node.typeName.getText(tree))
    ts.forEachChild(node, collect)
  }
  collect(property.type)
  if ([...references].some(name => !['Record', 'CronReasoningEffortOption'].includes(name))) throw new Error(`Inspect cron type dependencies: ${[...references]}`)
  const sibling = `${i18n}/types_cron.ts`
  if (fs.existsSync(path.join(root, sibling))) throw new Error(`Preserve ${sibling}`)
  write(sibling, `import type { CronReasoningEffortOption } from '@/types/hermes'\n\nexport type CronTranslations = ${property.type.getText(tree)}\n`)
  const updated = replaceRange(source, property, 'cron: CronTranslations', tree)
    .replace("import type { CronReasoningEffortOption } from '@/types/hermes'\n", '')
  write(relative, "import type { CronTranslations } from './types_cron'\n" + updated)
}

{
  const relative = 'apps/desktop/src/app/cron/index.tsx'
  const source = fs.readFileSync(path.join(root, relative), 'utf8')
  const expression = '{c.reasoningLabels[value] ?? value}'
  if (source.split(expression).length !== 2) throw new Error('Inspect the typed reasoning label expression')
  write(relative, source.replace(expression, '{c.reasoningLabels[value]}'))
}

const result = { files: written, completed: true }
fs.writeFileSync(path.join(root, 'artifacts/hermes-repair-20261019/desktop-source-owner-paths.json'), JSON.stringify(result))
process.stdout.write(JSON.stringify({ files: written, sha256: written.map(name => ({ path: name, sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex') })) }) + '\n')
