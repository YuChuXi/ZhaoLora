import { useEffect, useMemo, useState } from 'react';
import type { CaptionDirsData, CaptionGroup, CaptionRule, CaptionSettings } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Input, Select } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/controls';
import { Dialog } from '@/components/ui/dialog';

const MODE_LABELS: Record<string, string> = { coarse: '粗略', detailed: '详细' };

function templateVars(source: string): string[] {
  return Array.from(source.matchAll(/\{\{\s*(\w+)\s*\}\}/g)).map((match) => match[1]);
}

function fillTemplate(source: string, values: Record<string, string>): string {
  return source.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, name: string) => values[name] ?? '');
}

export function CaptionSettingsDialog({
  open,
  data,
  value,
  onClose,
  onSave,
}: {
  open: boolean;
  data: CaptionDirsData | null;
  value: CaptionSettings;
  onClose: () => void;
  onSave: (settings: CaptionSettings) => void;
}) {
  const [draft, setDraft] = useState<CaptionSettings>(value);
  const [template, setTemplate] = useState('');
  const [vars, setVars] = useState<Record<string, string>>({});

  useEffect(() => {
    if (open) setDraft(value);
  }, [open, value]);

  const templateSource = useMemo(
    () => data?.rule_templates.find((item) => item.label === template)?.text ?? '',
    [data, template],
  );
  const varNames = useMemo(() => templateVars(templateSource), [templateSource]);

  const patch = (next: Partial<CaptionSettings>) => setDraft({ ...draft, ...next });
  const setInject = (next: Partial<CaptionSettings['inject']>) =>
    setDraft({ ...draft, inject: { ...draft.inject, ...next } });

  const addRule = () => {
    patch({ rules: [...draft.rules, { text: '', pattern: '' }] });
  };
  const removeRule = (index: number) => {
    patch({ rules: draft.rules.filter((_, i) => i !== index) });
  };
  const updateRule = (index: number, next: Partial<CaptionRule>) => {
    patch({ rules: draft.rules.map((rule, i) => (i === index ? { ...rule, ...next } : rule)) });
  };

  const addGroup = () => {
    patch({ groups: [...draft.groups, { pattern: '', exclusive: true, items: [{ text: '', weight: 1 }] }] });
  };
  const removeGroup = (index: number) => {
    patch({ groups: draft.groups.filter((_, i) => i !== index) });
  };
  const updateGroup = (index: number, next: Partial<CaptionGroup>) => {
    patch({ groups: draft.groups.map((group, i) => (i === index ? { ...group, ...next } : group)) });
  };

  const applyTemplate = () => {
    if (!templateSource) return;
    patch({ rules: [...draft.rules, { pattern: '', text: fillTemplate(templateSource, vars) }] });
    setVars({});
  };

  const addLanguageSample = () => {
    patch({
      groups: [
        ...draft.groups,
        {
          pattern: '',
          exclusive: true,
          items: [
            { text: 'Using English', weight: 1 },
            { text: '使用中文', weight: 1 },
          ],
        },
      ],
    });
  };

  const closeWithSave = () => {
    onSave(draft);
    onClose();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) closeWithSave();
      }}
      title="描述设置"
      className="w-[min(860px,94vw)]"
      footer={
        <>
          <Button onClick={closeWithSave}>关闭</Button>
          <Button variant="primary" onClick={closeWithSave}>
            保存
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4 text-xs text-muted-foreground">
        <section className="flex flex-wrap items-center gap-3">
          <span className="w-16 text-foreground">描述模式</span>
          {(data?.modes ?? ['coarse', 'detailed']).map((mode) => (
            <label key={mode} className="flex items-center gap-1.5">
              <Checkbox
                checked={draft.modes.includes(mode)}
                onCheckedChange={(checked) =>
                  patch({
                    modes: checked
                      ? [...draft.modes, mode]
                      : draft.modes.filter((item) => item !== mode),
                  })
                }
              />
              {MODE_LABELS[mode] || mode}
            </label>
          ))}
          <span>写入 captions.json 时按此顺序排列</span>
        </section>

        <section className="flex flex-wrap items-center gap-3 border-t border-border pt-3">
          <span className="w-16 text-foreground">WD14 注入</span>
          <label className="flex items-center gap-1.5">
            <Checkbox
              checked={!!draft.inject.general}
              onCheckedChange={(checked) => setInject({ general: checked })}
            />
            通用
          </label>
          <Input
            type="number"
            min={0}
            max={1}
            step={0.05}
            className="w-16"
            value={draft.inject.general_threshold ?? ''}
            disabled={!draft.inject.general}
            onChange={(event) => setInject({ general_threshold: event.target.value })}
          />
          <label className="flex items-center gap-1.5">
            <Checkbox
              checked={!!draft.inject.character}
              onCheckedChange={(checked) => setInject({ character: checked })}
            />
            角色
          </label>
          <Input
            type="number"
            min={0}
            max={1}
            step={0.05}
            className="w-16"
            value={draft.inject.character_threshold ?? ''}
            disabled={!draft.inject.character}
            onChange={(event) => setInject({ character_threshold: event.target.value })}
          />
          <label className="flex items-center gap-1.5">
            <Checkbox
              checked={!!draft.inject.nsfw}
              onCheckedChange={(checked) => setInject({ nsfw: checked })}
            />
            NSFW 增强
          </label>
          <Input
            type="number"
            min={0}
            max={1}
            step={0.05}
            className="w-16"
            value={draft.inject.nsfw_threshold ?? ''}
            disabled={!draft.inject.nsfw}
            onChange={(event) => setInject({ nsfw_threshold: event.target.value })}
          />
        </section>

        <section className="flex flex-wrap items-center gap-3 border-t border-border pt-3">
          <span className="w-16 text-foreground">已有描述</span>
          <label className="flex items-center gap-1.5">
            <Checkbox
              checked={draft.force}
              onCheckedChange={(checked) => patch({ force: checked })}
            />
            清空已有描述后重新标注
          </label>
        </section>

        <section className="flex flex-col gap-2 border-t border-border pt-3">
          <div className="flex items-center justify-between">
            <span className="text-foreground">提示词条目</span>
            <Button size="sm" onClick={addRule}>
              添加条目
            </Button>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Select
              value={template}
              onChange={(event) => {
                setTemplate(event.target.value);
                setVars({});
              }}
              className="w-56"
            >
              <option value="">选择模板</option>
              {data?.rule_templates.map((item) => (
                <option key={item.label} value={item.label}>
                  {item.label}
                </option>
              ))}
            </Select>
            {varNames.map((name) => (
              <Input
                key={name}
                className="w-32"
                placeholder={name}
                value={vars[name] ?? ''}
                onChange={(event) => setVars({ ...vars, [name]: event.target.value })}
              />
            ))}
            <Button size="sm" disabled={!templateSource} onClick={applyTemplate}>
              套用
            </Button>
          </div>
          <div className="flex flex-col gap-1.5">
            {draft.rules.map((rule, index) => (
              <div key={index} className="flex items-center gap-2">
                <Input
                  className="w-48"
                  placeholder="匹配标签，如 *tail*"
                  value={rule.pattern}
                  onChange={(event) => updateRule(index, { pattern: event.target.value })}
                />
                <Input
                  className="flex-1"
                  placeholder="提示词内容"
                  value={rule.text}
                  onChange={(event) => updateRule(index, { text: event.target.value })}
                />
                <Button size="sm" variant="ghost" onClick={() => removeRule(index)}>
                  ✕
                </Button>
              </div>
            ))}
          </div>
        </section>

        <section className="flex flex-col gap-2 border-t border-border pt-3">
          <div className="flex items-center justify-between">
            <span className="text-foreground">随机抽取组</span>
            <div className="flex gap-2">
              <Button size="sm" onClick={addLanguageSample}>
                语言样例
              </Button>
              <Button size="sm" onClick={addGroup}>
                添加组
              </Button>
            </div>
          </div>
          {draft.groups.map((group, groupIndex) => (
            <div key={groupIndex} className="flex flex-col gap-1.5 rounded border border-border p-2">
              <div className="flex items-center gap-2">
                <Input
                  className="flex-1"
                  placeholder="匹配标签，如 *tail*"
                  value={group.pattern}
                  onChange={(event) => updateGroup(groupIndex, { pattern: event.target.value })}
                />
                <label className="flex items-center gap-1.5">
                  <Checkbox
                    checked={group.exclusive}
                    onCheckedChange={(checked) => updateGroup(groupIndex, { exclusive: checked })}
                  />
                  互斥
                </label>
                <Button size="sm" variant="ghost" onClick={() => removeGroup(groupIndex)}>
                  ✕
                </Button>
              </div>
              {group.items.map((item, itemIndex) => (
                <div key={itemIndex} className="flex items-center gap-2 pl-4">
                  <Input
                    className="flex-1"
                    placeholder="候选提示词"
                    value={item.text}
                    onChange={(event) =>
                      updateGroup(groupIndex, {
                        items: group.items.map((entry, i) =>
                          i === itemIndex ? { ...entry, text: event.target.value } : entry,
                        ),
                      })
                    }
                  />
                  <Input
                    type="number"
                    min={0}
                    step={0.1}
                    className="w-20"
                    value={item.weight}
                    onChange={(event) =>
                      updateGroup(groupIndex, {
                        items: group.items.map((entry, i) =>
                          i === itemIndex ? { ...entry, weight: Number(event.target.value) } : entry,
                        ),
                      })
                    }
                  />
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() =>
                      updateGroup(groupIndex, {
                        items: group.items.filter((_, i) => i !== itemIndex),
                      })
                    }
                  >
                    ✕
                  </Button>
                </div>
              ))}
              <Button
                size="sm"
                variant="ghost"
                onClick={() => updateGroup(groupIndex, { items: [...group.items, { text: '', weight: 1 }] })}
              >
                + 候选
              </Button>
            </div>
          ))}
        </section>
      </div>
    </Dialog>
  );
}
