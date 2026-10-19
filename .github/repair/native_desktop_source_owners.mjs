import fs from 'node:fs'
import path from 'node:path'
import crypto from 'node:crypto'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'

const require = createRequire(path.join(process.cwd(), 'package.json'))
const ts = require('typescript')
const root = process.cwd()
const i18n = 'apps/desktop/src/i18n'
const written = []
const planned = new Map()
const original = new Map()

function parsed(relative) {
  const source = execFileSync('git', ['show', `HEAD:${relative}`], { cwd: root, encoding: 'utf8' })
  original.set(relative, source)
  return { source, tree: ts.createSourceFile(relative, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS) }
}

function write(relative, source) {
  if (planned.has(relative)) throw new Error(`Inspect duplicate output ${relative}`)
  planned.set(relative, source)
  written.push(relative)
}

function replaceRange(source, node, replacement, tree) {
  return source.slice(0, node.getStart(tree)) + replacement + source.slice(node.getEnd())
}

for (const locale of ['en', 'ja', 'zh']) {
  const relative = `${i18n}/${locale}.ts`
  const { source, tree } = parsed(relative)
  const declaration = tree.statements.filter(ts.isVariableStatement).flatMap(statement => [...statement.declarationList.declarations])
    .find(node => node.name.getText(tree) === (locale === 'en' ? locale : `${locale}Overrides`))
  if (!declaration) throw new Error(`Inspect ${relative}`)
  let object = declaration.initializer
  while (ts.isSatisfiesExpression(object) || ts.isAsExpression(object) || ts.isParenthesizedExpression(object)) object = object.expression
  if (!ts.isObjectLiteralExpression(object)) throw new Error(`Inspect ${relative}`)
  const property = object.properties.find(node => ts.isPropertyAssignment(node) && node.name.getText(tree) === 'cron')
  if (!property || !ts.isObjectLiteralExpression(property.initializer)) throw new Error(`Inspect cron locale ${locale}`)
  const name = `${locale}Cron`
  const sibling = `${i18n}/${locale}_cron.ts`
  const importType = locale === 'en' ? "import type { CronTranslations } from './types_cron'" : "import type { TranslationOverrides } from './define-locale'"
  const declarationType = locale === 'en' ? ': CronTranslations' : ''
  const satisfies = locale === 'en' ? '' : " satisfies NonNullable<TranslationOverrides['cron']>"
  write(sibling, `${importType}\n\nexport const ${name}${declarationType} = ${property.initializer.getText(tree)}${satisfies}\n`)
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
  write(sibling, `import type { CronReasoningEffortOption } from '@/types/hermes'\n\nexport type CronTranslations = ${property.type.getText(tree)}\n`)
  const updated = replaceRange(source, property, 'cron: CronTranslations', tree)
    .replace("import type { CronReasoningEffortOption } from '@/types/hermes'\n", '')
  write(relative, "import type { CronTranslations } from './types_cron'\n" + updated)
}

{
  const relative = 'apps/desktop/src/app/cron/index.tsx'
  const { source } = parsed(relative)
  const expression = '{c.reasoningLabels[value] ?? value}'
  if (source.split(expression).length !== 2) throw new Error('Inspect the typed reasoning label expression')
  write(relative, source.replace(expression, '{c.reasoningLabels[value]}'))
}

for (const [relative, source] of planned) {
  const destination = path.join(root, relative)
  if (!fs.existsSync(destination)) {
    if (original.has(relative)) throw new Error(`Preserve the missing tracked file ${relative}`)
    continue
  }
  const current = fs.readFileSync(destination, 'utf8')
  if (current !== source && current !== original.get(relative)) throw new Error(`Preserve changed source ${relative}`)
}
for (const [relative, source] of planned) fs.writeFileSync(path.join(root, relative), source)
const result = { files: written, completed: true }
fs.writeFileSync(path.join(root, 'artifacts/hermes-repair-20261019/desktop-source-owner-paths.json'), JSON.stringify(result))
process.stdout.write(JSON.stringify({ files: written, sha256: written.map(name => ({ path: name, sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(root, name))).digest('hex') })) }) + '\n')
