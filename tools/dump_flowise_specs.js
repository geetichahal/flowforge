// Dumps exact node class definitions from a built Flowise checkout into
// flowforge/data/flowise_node_specs.json. Usage:
//   node tools/dump_flowise_specs.js /path/to/Flowise
// Node >=25 removed SlowBuffer; an old transitive dep (buffer-equal-constant-time) still reads it.
const buffer = require('buffer')
if (!buffer.SlowBuffer) buffer.SlowBuffer = buffer.Buffer
const fs = require('fs')
const path = require('path')
const root = process.argv[2] || path.join(process.env.HOME, 'Flowise')
const dist = path.join(root, 'packages/components/dist/nodes')
const wanted = {
  agentflow: ['Start', 'LLM', 'Agent', 'Tool', 'HTTP', 'Condition', 'CustomFunction', 'DirectReply', 'StickyNote'],
  tools: ['Gmail', 'GoogleDrive', 'GoogleSheets', 'GoogleCalendar', 'Jira', 'AWSSNS']
}
const keep = ['label', 'name', 'version', 'type', 'color', 'category', 'description', 'baseClasses',
  'inputs', 'outputs', 'hideOutput', 'hideInput', 'credential', 'icon']
const out = { _source: 'Flowise build (packages/components/dist)', _generated: new Date().toISOString(), nodes: {} }
for (const [dir, names] of Object.entries(wanted)) {
  for (const n of names) {
    const file = path.join(dist, dir, n, `${n}.js`)
    try {
      const mod = require(file)
      const inst = new mod.nodeClass()
      const spec = {}
      for (const k of keep) if (inst[k] !== undefined) spec[k] = inst[k]
      spec.icon = spec.icon ? path.basename(spec.icon) : undefined
      out.nodes[inst.name] = JSON.parse(JSON.stringify(spec))
      console.error('ok', inst.name, inst.version)
    } catch (e) {
      console.error('FAILED', file, e.message.split('\n')[0])
    }
  }
}
const dest = path.join(__dirname, '..', 'flowforge', 'data', 'flowise_node_specs.json')
fs.writeFileSync(dest, JSON.stringify(out, null, 1))
console.error('wrote', dest)
