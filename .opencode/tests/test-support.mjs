import { readFile } from "node:fs/promises"
import { stripTypeScriptTypes } from "node:module"
import { join } from "node:path"
import { createContext, SourceTextModule, SyntheticModule } from "node:vm"

async function source(path) {
  const url = new URL(path, import.meta.url)
  return stripTypeScriptTypes(await readFile(url, "utf8"), { sourceUrl: url.href })
}

// The helper is pure. Loading its stripped source needs neither dependencies nor HTTP.
export const pii = await import(
  `data:text/javascript,${encodeURIComponent(await source("../lib/confluence-pii.ts"))}`
)

export function page(overrides = {}) {
  return {
    id: "6160386",
    title: "Synthetic requirements",
    version: { number: 17, createdAt: "2026-11-14T10:20:30.000Z" },
    body: { storage: { value: "<p>AUTH-001: example requirement.</p>" } },
    ...overrides,
  }
}

// Only the two source files above are read from disk. The plugin's imports,
// environment and fetch are isolated; actual credentials are never accessible.
export async function pluginHarness(options = {}) {
  const reads = []
  const requests = []
  const env = options.env ?? {
    CONFLUENCE_URL: "https://example.atlassian.net/",
    CONFLUENCE_EMAIL: "synthetic@example.test",
    CONFLUENCE_API_TOKEN: "SYNTHETIC_API_TOKEN",
  }
  const context = createContext({
    Buffer,
    process: { env },
    fetch: async (url, init) => {
      requests.push({ url, init })
      if (options.fetch) return options.fetch(url, init)
      return { ok: true, json: async () => options.data ?? page() }
    },
  })
  const exports = {
    "@opencode/plugin": { Plugin: { define: (definition) => definition } },
    "node:fs/promises": {
      readFile: async (path, encoding) => {
        reads.push({ path, encoding })
        if (options.readFile) return options.readFile(path, encoding)
        throw Object.assign(new Error("Synthetic missing file"), { code: "ENOENT" })
      },
    },
    "node:os": { homedir: () => "/synthetic-home" },
    "node:path": { join },
    "../lib/confluence-pii": { maskPII: pii.maskPII, maskStoragePII: pii.maskStoragePII },
  }
  const module = new SourceTextModule(await source("../plugins/confluence.ts"), { context })
  await module.link((specifier) => {
    const values = exports[specifier]
    if (!values) throw new Error(`Unexpected plugin import: ${specifier}`)
    return new SyntheticModule(Object.keys(values), function () {
      for (const [name, value] of Object.entries(values)) this.setExport(name, value)
    }, { context })
  })
  await module.evaluate()
  let tool
  await module.namespace.default.setup({
    tool: { transform: async (transform) => transform({ add: (value) => { tool = value } }) },
  })
  return { plugin: module.namespace.default, tool, env, reads, requests }
}
