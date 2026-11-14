/** SAM3 蒙板表达式的词法切分与编辑辅助. */

/** 表达式词法单元. */
export interface ExpressionToken {
  k: 'str' | 'op' | 'pun' | 'semi' | 'atom';
  v: string;
}

/**
 * 词法切分蒙板表达式.
 *
 * 全角引号先归一为 ASCII 引号；字符串为原子 token 并剥离引号；
 * 其余切分为运算符、括号、逗号、分号（含全角）与裸词/数字；空白丢弃.
 *
 * Args:
 *   text (string): 原始表达式.
 *
 * Returns:
 *   ExpressionToken[]: 词法单元序列.
 *
 * Callers:
 *   - `frontend/src/pages/paint/expression.ts:expandSemicolons`
 *   - `frontend/src/pages/paint/expression.ts:formatExpression`
 */
export function tokenizeExpression(text: string): ExpressionToken[] {
  const src = text.replace(/\u201c/g, '"').replace(/\u201d/g, '"');
  const tokens: ExpressionToken[] = [];
  let i = 0;
  while (i < src.length) {
    const ch = src[i];
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
      i += 1;
      continue;
    }
    if (ch === '"') {
      const end = src.indexOf('"', i + 1);
      if (end < 0) {
        tokens.push({ k: 'str', v: src.slice(i + 1) });
        break;
      }
      tokens.push({ k: 'str', v: src.slice(i + 1, end) });
      i = end + 1;
      continue;
    }
    const two = src.slice(i, i + 2);
    if (two === '>=' || two === '<=' || two === '==' || two === '!=') {
      tokens.push({ k: 'op', v: two });
      i += 2;
      continue;
    }
    if ('+-*<>'.indexOf(ch) >= 0) {
      tokens.push({ k: 'op', v: ch });
      i += 1;
      continue;
    }
    if (ch === '(' || ch === ')' || ch === ',') {
      tokens.push({ k: 'pun', v: ch });
      i += 1;
      continue;
    }
    if (ch === ';' || ch === '\uff1b') {
      tokens.push({ k: 'semi', v: ';' });
      i += 1;
      continue;
    }
    let j = i;
    while (j < src.length && ' \t\n\r"+-*<>(),;\uff1b'.indexOf(src[j]) < 0) j += 1;
    tokens.push({ k: 'atom', v: src.slice(i, j) });
    i = j;
  }
  return tokens;
}

/**
 * 把词法单元序列化回表达式文本.
 *
 * Args:
 *   tokens (ExpressionToken[]): 词法单元序列.
 *
 * Returns:
 *   string: 字符串加回引号，逗号后补一个空格，其余紧连.
 *
 * Callers:
 *   - `frontend/src/pages/paint/expression.ts:expandSemicolons`
 */
function serializeTokens(tokens: ExpressionToken[]): string {
  let out = '';
  for (const token of tokens) {
    out += token.k === 'str' ? `"${token.v}"` : token.k === 'pun' && token.v === ',' ? ', ' : token.v;
  }
  return out;
}

/**
 * 把表达式中的分号列表展开为字符串相加.
 *
 * 识别三种形态：引号内分号、既有括号内分号、顶层分号；全角分号同样识别；
 * 与分号相邻的裸词概念自动加引号（`当前蒙板` 与数值除外）.
 *
 * Args:
 *   text (string): 原始表达式.
 *
 * Returns:
 *   string | null: 展开后的表达式；无分号时为 null.
 *
 * Callers:
 *   - `frontend/src/pages/paint/MaskGenerateDialog.tsx:handleExpand`
 */
export function expandSemicolons(text: string): string | null {
  const tokens = tokenizeExpression(text);
  const hasSemi = tokens.some(
    (token) => token.k === 'semi' || (token.k === 'str' && /[;；]/.test(token.v)),
  );
  if (!hasSemi) return null;
  const pass1: ExpressionToken[] = [];
  for (const token of tokens) {
    if (token.k !== 'str' || !/[;；]/.test(token.v)) {
      pass1.push(token);
      continue;
    }
    const parts = token.v
      .split(/[;；]/)
      .map((part) => part.trim())
      .filter((part) => part);
    if (parts.length === 1) {
      pass1.push({ k: 'str', v: parts[0] });
      continue;
    }
    if (parts.length >= 2) {
      const prev = pass1[pass1.length - 1];
      const wrapped = !(prev && prev.k === 'pun' && prev.v === '(');
      if (wrapped) pass1.push({ k: 'pun', v: '(' });
      pass1.push({ k: 'str', v: parts[0] });
      for (let p = 1; p < parts.length; p += 1) {
        pass1.push({ k: 'op', v: '+' });
        pass1.push({ k: 'str', v: parts[p] });
      }
      if (wrapped) pass1.push({ k: 'pun', v: ')' });
      continue;
    }
    pass1.push(token);
  }
  if (!pass1.some((token) => token.k === 'atom' || token.k === 'str')) return text;
  for (let s = 0; s < pass1.length; s += 1) {
    if (pass1[s].k !== 'semi') continue;
    for (let d = s - 1; d <= s + 1; d += 2) {
      const neighbor = pass1[d];
      if (
        neighbor &&
        neighbor.k === 'atom' &&
        neighbor.v !== '当前蒙板' &&
        !/^-?\d+(\.\d+)?$/.test(neighbor.v)
      ) {
        pass1[d] = { k: 'str', v: neighbor.v };
      }
    }
  }
  let depth = 0;
  let topLevelSemi = false;
  const runs: ExpressionToken[][] = [[]];
  for (const token of pass1) {
    if (token.k === 'semi') {
      if (depth === 0) topLevelSemi = true;
      runs.push([]);
      continue;
    }
    if (token.k === 'pun' && token.v === '(') depth += 1;
    else if (token.k === 'pun' && token.v === ')') depth -= 1;
    runs[runs.length - 1].push(token);
  }
  const items = runs
    .map((run) => serializeTokens(run).trim())
    .filter((segment) => segment);
  if (!items.length) return text;
  const out = items.join('+');
  return topLevelSemi ? `(${out})` : out;
}

/**
 * 按括号缩进重排表达式.
 *
 * 分组括号左括号留在行尾、内容换行缩进（每层 4 空格）、右括号独立成行；
 * 函数调用括号不独立成行；运算符尾随左操作数；字符串内容原样保留.
 *
 * Args:
 *   text (string): 原始表达式（可含任意空白排版）.
 *
 * Returns:
 *   string | null: 重排后的多行表达式；空表达式为 null.
 *
 * Callers:
 *   - `frontend/src/pages/paint/MaskGenerateDialog.tsx:handleFormat`
 */
export function formatExpression(text: string): string | null {
  const tokens = tokenizeExpression(text);
  if (!tokens.length) return null;
  const indent = '    ';
  const lines: string[] = [];
  let line = '';
  let depth = 0;
  const calls: boolean[] = [];
  let prev: ExpressionToken | null = null;
  let afterOp = false;
  const pushLine = () => {
    const trimmed = line.replace(/\s+$/, '');
    if (trimmed) lines.push(trimmed);
    line = '';
  };
  const newLine = () => {
    pushLine();
    line = indent.repeat(depth);
  };
  for (const token of tokens) {
    if (token.k === 'pun' && token.v === '(') {
      line += '(';
      depth += 1;
      calls.push(prev !== null && prev.k === 'atom');
      if (!calls[calls.length - 1]) newLine();
      prev = token;
      afterOp = false;
      continue;
    }
    if (token.k === 'pun' && token.v === ')') {
      depth -= 1;
      const isCall = calls.pop();
      if (!isCall) newLine();
      line += ')';
      prev = token;
      afterOp = false;
      continue;
    }
    if (token.k === 'pun' && token.v === ',') {
      line += ', ';
      prev = token;
      afterOp = false;
      continue;
    }
    const isBinary =
      token.k === 'op' &&
      prev !== null &&
      !(prev.k === 'op' || (prev.k === 'pun' && (prev.v === '(' || prev.v === ',')));
    if (isBinary) {
      line += ` ${token.v} `;
      prev = token;
      afterOp = true;
      continue;
    }
    if (afterOp && depth > 0) newLine();
    line += token.k === 'str' ? `"${token.v}"` : token.v;
    prev = token;
    afterOp = false;
  }
  pushLine();
  return lines.join('\n');
}

/**
 * 折叠引号外空白为单空格并去首尾空白.
 *
 * Args:
 *   text (string): 输入框中的表达式（可能多行）.
 *
 * Returns:
 *   string: 折叠后的单行表达式.
 *
 * Callers:
 *   - `frontend/src/pages/paint/MaskGenerateDialog.tsx:handleStart`
 */
export function flattenExpression(text: string): string {
  let out = '';
  let inStr = false;
  let spaced = false;
  for (const ch of text) {
    if (inStr) {
      out += ch;
      if (ch === '"' || ch === '\u201c' || ch === '\u201d') inStr = false;
      continue;
    }
    if (ch === '"' || ch === '\u201c' || ch === '\u201d') {
      inStr = true;
      out += ch;
      continue;
    }
    if (ch === ' ' || ch === '\t' || ch === '\n' || ch === '\r') {
      if (out && !spaced) {
        out += ' ';
        spaced = true;
      }
      continue;
    }
    out += ch;
    spaced = false;
  }
  return out.trim();
}
