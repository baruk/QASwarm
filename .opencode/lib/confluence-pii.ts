type PIIType = "PASSWORD" | "TOKEN" | "EMAIL" | "PHONE"
type Span = { start: number; end: number }
type Redaction = Span & { type: PIIType }
type Edit = Span & { replacement: string }
type DecodedText = { text: string; offsets: (Span | null)[] }

export type PIIResult = {
  piiFound: boolean
  detectedPII: PIIType[]
  maskedText: string
}

const secretAssignment = /(?<![\p{L}\p{N}_])(["'`]?)(password|passwd|пароль|api[\p{Zs}\t_-]*key|access[\p{Zs}\t_-]*token|refresh[\p{Zs}\t_-]*token|client[\p{Zs}\t_-]*secret|token|токен|authorization)\1[\p{Zs}\t]*[:=][\p{Zs}\t]*/giu
const emailPattern = /(?<![\p{L}\p{N}._%+-])[\p{L}\p{N}._%+-]+@(?:[\p{L}\p{N}-]+\.)+[\p{L}]{2,63}(?![\p{L}\p{N}-])/gu
const phoneCandidate = /(?<![\p{L}\p{N}_])(?:\+?\d|\(\d)[\d\p{Zs}\t().-]{5,40}\d(?![\p{L}\p{N}_])/gu
const phoneContext = /(?:^|[^\p{L}\p{N}_])(?:phone|telephone|tel|mobile|телефон|мобильный)["'`]?[\p{Zs}\t]*[:=][\p{Zs}\t]*["'`]?[\p{Zs}\t]*$/iu
const marker = /^\[(?:PASSWORD|TOKEN|EMAIL|PHONE)_MASKED\]$/
const categories: PIIType[] = ["PASSWORD", "TOKEN", "EMAIL", "PHONE"]

// Confluence storage — XHTML. Декодируем текст для поиска, но сохраняем исходную
// разметку и позиции. Это эвристика по известным шаблонам, не полноценный DLP:
// немаркированный секрет или голый цифровой номер нельзя надёжно отличить от ID.
const entities: Record<string, string> = {
  amp: "&", apos: "'", quot: '"', lt: "<", gt: ">", nbsp: "\u00a0",
  colon: ":", equals: "=", commat: "@", period: ".", plus: "+",
  lpar: "(", rpar: ")", hyphen: "-", sol: "/", bsol: "\\",
  semi: ";", tab: "\t", newline: "\n",
}

function decodeText(raw: string, offset = 0, decodeEntities = true): DecodedText {
  let text = ""
  const offsets: (Span | null)[] = []
  let cursor = 0
  const appendLiteral = (start: number, end: number) => {
    text += raw.slice(start, end)
    for (let index = start; index < end; index++) {
      offsets.push({ start: offset + index, end: offset + index + 1 })
    }
  }
  if (!decodeEntities) {
    appendLiteral(0, raw.length)
    return { text, offsets }
  }
  for (const match of raw.matchAll(/&(?:#(?:x[0-9a-f]+|\d+)|[a-z]+);/gi)) {
    appendLiteral(cursor, match.index)
    const name = match[0].slice(1, -1)
    let value = entities[name.toLowerCase()]
    if (name.startsWith("#")) {
      const hex = name[1]?.toLowerCase() === "x"
      const code = Number.parseInt(name.slice(hex ? 2 : 1), hex ? 16 : 10)
      if (code > 0 && code <= 0x10ffff && !(code >= 0xd800 && code <= 0xdfff)) {
        value = String.fromCodePoint(code)
      }
    }
    if (value === undefined) {
      appendLiteral(match.index, match.index + match[0].length)
    } else {
      text += value
      for (let index = 0; index < value.length; index++) {
        offsets.push({ start: offset + match.index, end: offset + match.index + match[0].length })
      }
    }
    cursor = match.index + match[0].length
  }
  appendLiteral(cursor, raw.length)
  return { text, offsets }
}

function fieldType(field: string): PIIType | undefined {
  const name = field.toLowerCase().replace(/^data-/, "").replace(/[\p{Zs}\t_-]/gu, "")
  if (["password", "passwd", "пароль"].includes(name)) return "PASSWORD"
  if (["apikey", "accesstoken", "refreshtoken", "clientsecret", "token", "токен", "authorization"].includes(name)) return "TOKEN"
  if (["phone", "telephone", "tel", "mobile", "телефон", "мобильный"].includes(name)) return "PHONE"
}

function findRedactions(text: string, field = ""): Redaction[] {
  const found: Redaction[] = []
  const add = (type: PIIType, start: number, end: number) => {
    if (start >= end || found.some(span => start < span.end && end > span.start)) return
    found.push({ type, start, end })
  }
  const attributeType = fieldType(field)
  if (attributeType === "PASSWORD" || attributeType === "TOKEN") {
    if (text.trim() && !marker.test(text.trim())) add(attributeType, 0, text.length)
    return found
  }

  const assignments = new RegExp(secretAssignment.source, secretAssignment.flags)
  let match: RegExpExecArray | null
  while ((match = assignments.exec(text)) !== null) {
    let start = match.index + match[0].length
    let end = start
    const quote = text[start]
    if (quote === '"' || quote === "'" || quote === "`") {
      start++
      end = start
      while (end < text.length && text[end] !== quote) {
        end += text[end] === "\\" ? 2 : 1
      }
      end = Math.min(end, text.length)
      assignments.lastIndex = end < text.length ? end + 1 : end
    } else {
      // '&' разделяет параметры URL, но может быть частью обычного пароля.
      // Без кавычек берём остаток строки до следующего подписанного секрета.
      const inQuery = /[?&]$/.test(text.slice(0, match.index)) && match[0].includes("=")
      const delimiter = text.slice(start).search(inQuery ? /[&#\s]/ : /[\r\n]/)
      end = delimiter === -1 ? text.length : start + delimiter
      // Несколько подписанных полей могут идти подряд без запятой.
      const next = new RegExp(secretAssignment.source, secretAssignment.flags).exec(text.slice(start, end))
      if (next) {
        end = start + next.index
        while (end > start && /\s/.test(text[end - 1])) end--
        if (/[;,]/.test(text[end - 1] ?? "")) end--
      }
      while (end > start && /\s/.test(text[end - 1])) end--
      assignments.lastIndex = Math.max(end, assignments.lastIndex)
    }
    if (!marker.test(text.slice(start, end).trim())) {
      add(fieldType(match[2]) as "PASSWORD" | "TOKEN", start, end)
    }
  }

  for (const email of text.matchAll(emailPattern)) {
    add("EMAIL", email.index, email.index + email[0].length)
  }
  for (const phone of text.matchAll(phoneCandidate)) {
    const value = phone[0].replace(/\p{Zs}/gu, " ")
    const digits = value.replace(/\D/g, "").length
    const explicit = attributeType === "PHONE" || phoneContext.test(text.slice(Math.max(0, phone.index - 80), phone.index))
    const formatted = value.startsWith("+")
      || /^(?:\d[ \t]*)?\(\d{2,5}\)[ \t]*\d{2,4}(?:[- \t]\d{2,4}){1,2}$/.test(value)
      || /^\d{3}[- \t]\d{3}[- \t]\d{4}$/.test(value)
    if (digits >= (explicit ? 7 : 10) && digits <= 15 && (explicit || formatted)) {
      add("PHONE", phone.index, phone.index + phone[0].length)
    }
  }
  return found.sort((left, right) => left.start - right.start)
}

function mappedEdits(decoded: DecodedText, redactions: Redaction[]): Edit[] {
  const edits: Edit[] = []
  for (const redaction of redactions) {
    const spans: Span[] = []
    for (let index = redaction.start; index < redaction.end; index++) {
      const span = decoded.offsets[index]
      if (!span) continue
      const previous = spans[spans.length - 1]
      if (previous && span.start <= previous.end) {
        previous.end = Math.max(previous.end, span.end)
      } else {
        spans.push({ ...span })
      }
    }
    spans.forEach((span, index) => edits.push({
      ...span,
      replacement: index === 0 ? `[${redaction.type}_MASKED]` : "",
    }))
  }
  return edits
}

function result(raw: string, edits: Edit[], detected: Set<PIIType>): PIIResult {
  let maskedText = ""
  let cursor = 0
  for (const edit of edits.sort((left, right) => left.start - right.start)) {
    maskedText += raw.slice(cursor, edit.start) + edit.replacement
    cursor = edit.end
  }
  maskedText += raw.slice(cursor)
  return { piiFound: detected.size > 0, detectedPII: categories.filter(type => detected.has(type)), maskedText }
}

export function maskPII(text: string): PIIResult {
  const decoded = decodeText(text)
  const redactions = findRedactions(decoded.text)
  return result(text, mappedEdits(decoded, redactions), new Set(redactions.map(span => span.type)))
}

export function maskStoragePII(storage: string): PIIResult {
  const visible: DecodedText = { text: "", offsets: [] }
  const edits: Edit[] = []
  const detected = new Set<PIIType>()
  const collect = (raw: string, offset: number, field = "", decodeEntities = true) => {
    const decoded = decodeText(raw, offset, decodeEntities)
    const redactions = findRedactions(decoded.text, field)
    redactions.forEach(span => detected.add(span.type))
    edits.push(...mappedEdits(decoded, redactions))
  }
  const appendText = (raw: string, offset: number, decodeEntities = true) => {
    const decoded = decodeText(raw, offset, decodeEntities)
    visible.text += decoded.text
    for (const span of decoded.offsets) visible.offsets.push(span)
  }

  // Токенизация storage без внешних зависимостей. '>' внутри кавычек атрибута
  // не завершает тег; inline-теги не разрывают пароль или email в видимом тексте.
  const tokens = /<!--[\s\S]*?(?:-->|$)|<!\[CDATA\[[\s\S]*?(?:\]\]>|$)|<(?:(?:"[^"]*"|'[^']*'|[^'">])*)>|[^<]+|</g
  for (const token of storage.matchAll(tokens)) {
    const raw = token[0]
    if (raw.startsWith("<!--")) {
      collect(raw.slice(4, raw.endsWith("-->") ? -3 : undefined), token.index + 4, "", false)
    } else if (raw.startsWith("<![CDATA[")) {
      appendText(raw.slice(9, raw.endsWith("]]>") ? -3 : undefined), token.index + 9, false)
    } else if (raw.startsWith("<") && raw.endsWith(">")) {
      const tag = /^<\/?([\w:-]+)/.exec(raw)?.[1]?.toLowerCase()
      if (tag && /^(?:p|div|h[1-6]|li|tr|td|th|br|hr|pre|table|ul|ol|blockquote|ac:structured-macro|ac:plain-text-body|ac:rich-text-body)$/.test(tag)) {
        visible.text += "\n"
        visible.offsets.push(null)
      }
      const attributes = raw.slice(0, raw.endsWith("/>") ? -2 : -1)
      for (const attribute of attributes.matchAll(/([\p{L}\p{N}_:.-]+)\s*=\s*("[^"]*"|'[^']*'|[^\s"'<>`=]+)/gu)) {
        const value = attribute[2]
        const quoted = value.startsWith('"') || value.startsWith("'")
        const start = token.index + attribute.index + attribute[0].length - value.length + (quoted ? 1 : 0)
        collect(quoted ? value.slice(1, -1) : value, start, attribute[1])
      }
    } else {
      appendText(raw, token.index)
    }
  }
  const redactions = findRedactions(visible.text)
  redactions.forEach(span => detected.add(span.type))
  edits.push(...mappedEdits(visible, redactions))
  return result(storage, edits, detected)
}
