import type { ConditionField, SourceCondition } from '@/lib/api';

/** 条件对话框的字段取值: chips 为数组, range 为 `{key}Lo` / `{key}Hi`, text 为字符串. */
export type ConditionValues = Record<string, string | string[]>;

const NUMERIC = /^(-?\d+)$/;
const DATE = /^(\d{4}-\d{2}-\d{2})$/;

/**
 * 生成条件字段的初始取值: chips 空数组、range 空串、text 空串.
 *
 * Args:
 *   condition (SourceCondition): 来源条件声明.
 *
 * Returns:
 *   ConditionValues: 全部字段的空取值.
 *
 * Callers:
 *   - `filter/condition.ts:parseCondition`
 *   - `filter/Dialogs.tsx:ConditionDialog`
 */
export function emptyCondition(condition: SourceCondition): ConditionValues {
  const values: ConditionValues = {};
  for (const field of condition.fields) {
    if (field.kind === 'chips') values[field.key] = [];
    else if (field.kind === 'range') {
      values[`${field.key}Lo`] = '';
      values[`${field.key}Hi`] = '';
    } else values[field.key] = '';
  }
  return values;
}

/**
 * 把条件串解析为字段取值, 未识别的 token 收进原样输出的文本字段.
 *
 * Args:
 *   condition (SourceCondition): 来源条件声明.
 *   extra (string): 条件串.
 *
 * Returns:
 *   ConditionValues: 字段取值.
 *
 * Callers:
 *   - `filter/Dialogs.tsx:ConditionDialog`
 *   - `filter/condition.ts:conditionSummary`
 */
export function parseCondition(condition: SourceCondition, extra: string): ConditionValues {
  const values = emptyCondition(condition);
  const raw = condition.fields.find((field) => field.kind === 'text' && field.raw);
  const rest: string[] = [];
  const fallback = (token: string) => {
    if (raw) rest.push(token);
  };

  for (const token of extra.split(/\s+/).filter(Boolean)) {
    let matched = false;
    for (const field of condition.fields) {
      if (field.kind === 'chips') {
        const negative = token.match(new RegExp(`^-${field.key}:([^,]+)$`, 'i'));
        if (negative) {
          const excluded = negative[1].toLowerCase();
          values[field.key] = field.options
            .map((option) => option.value)
            .filter((value) => value !== excluded);
          matched = true;
          break;
        }
        const positive = token.match(new RegExp(`^${field.key}:([^\\s]+)$`, 'i'));
        if (positive) {
          const known = new Set(field.options.map((option) => option.value));
          const picked = positive[1]
            .split(',')
            .map((value) => value.toLowerCase())
            .filter((value) => known.has(value));
          if (!picked.length) break;
          const current = Array.isArray(values[field.key])
            ? (values[field.key] as string[])
            : [];
          values[field.key] =
            field.join === 'repeat' ? Array.from(new Set([...current, ...picked])) : picked;
          matched = true;
          break;
        }
        continue;
      }

      if (field.kind === 'range') {
        if (field.lo_token || field.hi_token) {
          const bound = field.lo_token && token.startsWith(field.lo_token)
            ? { side: 'lo' as const, value: token.slice(field.lo_token.length) }
            : field.hi_token && token.startsWith(field.hi_token)
              ? { side: 'hi' as const, value: token.slice(field.hi_token.length) }
              : null;
          if (!bound) continue;
          if (!(field.date ? DATE.test(bound.value) : NUMERIC.test(bound.value))) continue;
          values[`${field.key}${bound.side === 'lo' ? 'Lo' : 'Hi'}`] = bound.value;
          matched = true;
          break;
        }
        const tokenMatch = token.match(new RegExp(`^${field.key}:(>=|<=|>|<|=)?(.+)$`, 'i'));
        if (!tokenMatch) continue;
        const value = tokenMatch[2];
        if (!(field.date ? DATE.test(value) : NUMERIC.test(value))) continue;
        const operator = tokenMatch[1] ?? '=';
        if (operator.startsWith('<')) values[`${field.key}Hi`] = value;
        else if (operator === '=') {
          values[`${field.key}Lo`] = value;
          values[`${field.key}Hi`] = value;
        } else values[`${field.key}Lo`] = value;
        matched = true;
        break;
      }

      if (field.raw) continue;
      const textMatch = token.match(new RegExp(`^${field.key}:(.+)$`, 'i'));
      if (!textMatch) continue;
      values[field.key] = textMatch[1];
      matched = true;
      break;
    }
    if (!matched) fallback(token);
  }

  if (raw) values[raw.key] = rest.join(' ');
  return values;
}

/**
 * 把字段取值序列化为条件串.
 *
 * chips 按声明的方式输出: `repeat` 逐项、`comma` 逗号 OR、`exclude` 三选二时
 * 排除剩余项; range 等值输出单 token, 否则分别输出上下限; raw 文本原样输出.
 *
 * Args:
 *   condition (SourceCondition): 来源条件声明.
 *   values (ConditionValues): 字段取值.
 *
 * Returns:
 *   string: 条件串.
 *
 * Callers:
 *   - `filter/Dialogs.tsx:ConditionDialog`
 */
export function buildCondition(condition: SourceCondition, values: ConditionValues): string {
  const parts: string[] = [];
  for (const field of condition.fields) {
    if (field.kind === 'chips') {
      const selected = Array.isArray(values[field.key])
        ? (values[field.key] as string[])
        : [];
      const all = field.options.map((option) => option.value);
      if (!selected.length) continue;
      if (field.join === 'exclude' && all.length === 3) {
        if (selected.length === all.length) continue;
        if (selected.length === 2) {
          const excluded = all.find((value) => !selected.includes(value));
          if (excluded) parts.push(`-${field.key}:${excluded}`);
        } else parts.push(`${field.key}:${selected[0]}`);
      } else if (field.join === 'comma') {
        parts.push(`${field.key}:${selected.join(',')}`);
      } else {
        for (const value of selected) parts.push(`${field.key}:${value}`);
      }
      continue;
    }

    if (field.kind === 'range') {
      const lo = String(values[`${field.key}Lo`] ?? '').trim();
      const hi = String(values[`${field.key}Hi`] ?? '').trim();
      if (field.lo_token || field.hi_token) {
        if (lo) parts.push(`${field.lo_token}${lo}`);
        if (hi) parts.push(`${field.hi_token}${hi}`);
        continue;
      }
      if (lo && hi && lo === hi) parts.push(`${field.key}:${lo}`);
      else {
        if (lo) parts.push(`${field.key}:>=${lo}`);
        if (hi) parts.push(`${field.key}:<=${hi}`);
      }
      continue;
    }

    const value = String(values[field.key] ?? '').trim();
    if (!value) continue;
    parts.push(field.raw ? value : `${field.key}:${value}`);
  }
  return parts.join(' ');
}

/**
 * 统计已生效条件项数并生成可读摘要.
 *
 * 每个 chip 计 1; 区间上下限分别计 1, 上下限相等时计 1; 文本非空计 1.
 *
 * Args:
 *   condition (SourceCondition): 来源条件声明.
 *   values (ConditionValues): 字段取值.
 *
 * Returns:
 *   { count: number; summary: string }: 条件项数与摘要.
 *
 * Callers:
 *   - `filter/Toolbar.tsx:SearchSection`
 */
export function conditionBadge(
  condition: SourceCondition,
  values: ConditionValues,
): { count: number; summary: string } {
  let count = 0;
  const bits: string[] = [];
  for (const field of condition.fields) {
    if (field.kind === 'chips') {
      const selected = Array.isArray(values[field.key])
        ? (values[field.key] as string[])
        : [];
      if (!selected.length) continue;
      count += selected.length;
      const labels = field.options
        .filter((option) => selected.includes(option.value))
        .map((option) => option.label);
      bits.push(`${field.label}:${labels.join('/')}`);
      continue;
    }

    if (field.kind === 'range') {
      const lo = String(values[`${field.key}Lo`] ?? '').trim();
      const hi = String(values[`${field.key}Hi`] ?? '').trim();
      if (!lo && !hi) continue;
      if (lo && hi && lo === hi) {
        count += 1;
        bits.push(`${field.label}=${lo}`);
      } else {
        if (lo) {
          count += 1;
          bits.push(`${field.label}≥${lo}`);
        }
        if (hi) {
          count += 1;
          bits.push(`${field.label}≤${hi}`);
        }
      }
      continue;
    }

    const value = String(values[field.key] ?? '').trim();
    if (!value) continue;
    count += 1;
    bits.push(`${field.label}:${value}`);
  }
  return { count, summary: bits.join(' · ') };
}

/**
 * 取字段的 range 上下限键名, 供对话框渲染.
 *
 * Args:
 *   field (ConditionField): range 字段声明.
 *   side ('lo' | 'hi'): 下限或上限.
 *
 * Returns:
 *   string: 取值键名.
 *
 * Callers:
 *   - `filter/Dialogs.tsx:ConditionDialog`
 *   - `filter/condition.ts:parseCondition`
 */
export function rangeKey(field: ConditionField, side: 'lo' | 'hi'): string {
  return `${field.key}${side === 'lo' ? 'Lo' : 'Hi'}`;
}
