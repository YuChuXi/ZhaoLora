/** 蒙版生成对话框（BiRefNet 背景移除 / SAM3 概念分割）. */

import { useState } from 'react';
import { apiPath, request } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/controls';
import { Dialog } from '@/components/ui/dialog';
import { Input, Select } from '@/components/ui/input';
import { expandSemicolons, flattenExpression, formatExpression } from './expression';

/** 生成方式. */
type MaskMode = 'birefnet' | 'sam3';

/** 蒙版生成对话框属性. */
interface MaskGenerateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentPath: string | null;
  dir: string;
  notify: (message: string) => void;
  onStarted: (jobId: string, batch: boolean) => void;
}

/**
 * 解析整数输入；非法输入回退到默认值.
 *
 * Args:
 *   value (string): 输入框文本.
 *   fallback (number): 非法时采用的默认值.
 *
 * Returns:
 *   number: 解析后的整数.
 *
 * Callers:
 *   - `frontend/src/pages/paint/MaskGenerateDialog.tsx:handleStart`
 */
function intOr(value: string, fallback: number): number {
  const parsed = Number.parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

/**
 * 蒙版生成表单：选择生成方式、配置参数并提交长任务.
 *
 * Args:
 *   props (MaskGenerateDialogProps): 当前图路径、目录、通知与任务启动回调.
 *
 * Returns:
 *   JSX.Element: 模态对话框.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:PaintPage`
 */
export function MaskGenerateDialog({
  open,
  onOpenChange,
  currentPath,
  dir,
  notify,
  onStarted,
}: MaskGenerateDialogProps) {
  const [mode, setMode] = useState<MaskMode>('birefnet');
  const [expand, setExpand] = useState('32');
  const [shrink, setShrink] = useState('0');
  const [min, setMin] = useState('96');
  const [max, setMax] = useState('255');
  const [expression, setExpression] = useState('(当前蒙板-("白色背景"+"对话气泡"))');
  const [batch, setBatch] = useState(false);
  const [skipExisting, setSkipExisting] = useState(true);
  const [submitting, setSubmitting] = useState(false);

  /**
   * 把分号概念列表展开为字符串相加并回写表达式.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 对话框「分号转相加」按钮.
   */
  const handleExpand = () => {
    const expanded = expandSemicolons(expression);
    if (expanded === null) {
      notify('表达式中没有分号');
      return;
    }
    setExpression(expanded);
  };

  /**
   * 按括号缩进重排表达式并回写输入框.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   void: 无返回值.
   *
   * Callers:
   *   - 对话框「缩进格式化」按钮.
   */
  const handleFormat = () => {
    const formatted = formatExpression(expression);
    if (formatted === null) {
      notify('表达式为空');
      return;
    }
    setExpression(formatted);
  };

  /**
   * 组装请求体并提交蒙版生成任务.
   *
   * Args:
   *   无.
   *
   * Returns:
   *   Promise<void>: 提交完成或错误已通知.
   *
   * Callers:
   *   - 对话框「开始」按钮.
   */
  const handleStart = async () => {
    if (submitting) return;
    const body: Record<string, unknown> = {
      dir,
      mode,
      skip_existing: batch ? skipExisting : false,
    };
    if (mode === 'sam3') {
      const flattened = flattenExpression(expression);
      if (!flattened) {
        notify('请输入蒙板表达式');
        return;
      }
      body.expression = flattened;
    } else {
      body.mask_expand = intOr(expand, 32);
      body.mask_shrink = intOr(shrink, 0);
      body.mask_min = intOr(min, 96);
      body.mask_max = intOr(max, 255);
    }
    if (!batch) {
      if (!currentPath) {
        notify('当前没有图片');
        return;
      }
      body.path = currentPath;
    }
    setSubmitting(true);
    try {
      const response = await request<{ ok: boolean; job_id: string }>(apiPath('/mask-generate'), {
        method: 'POST',
        body: JSON.stringify(body),
      });
      onOpenChange(false);
      onStarted(response.job_id, batch);
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="蒙版生成"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>取消</Button>
          <Button variant="primary" disabled={submitting} onClick={() => void handleStart()}>
            开始
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        <p className="text-xs text-muted-foreground">
          默认仅重新生成当前图片蒙版，勾选「批量」处理整个目录。
        </p>
        <label className="flex items-center gap-2 text-xs text-muted-foreground">
          生成方式
          <Select value={mode} onChange={(event) => setMode(event.target.value as MaskMode)}>
            <option value="birefnet">BiRefNet 背景移除</option>
            <option value="sam3">SAM3 概念分割</option>
          </Select>
        </label>

        {mode === 'birefnet' ? (
          <div className="flex flex-wrap items-center gap-2.5">
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              扩展像素
              <Input
                type="number"
                min={0}
                max={200}
                className="w-16"
                value={expand}
                onChange={(event) => setExpand(event.target.value)}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              收缩像素
              <Input
                type="number"
                min={0}
                max={200}
                className="w-16"
                value={shrink}
                onChange={(event) => setShrink(event.target.value)}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              最小值
              <Input
                type="number"
                min={0}
                max={255}
                className="w-16"
                value={min}
                onChange={(event) => setMin(event.target.value)}
              />
            </label>
            <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
              最大值
              <Input
                type="number"
                min={0}
                max={255}
                className="w-16"
                value={max}
                onChange={(event) => setMax(event.target.value)}
              />
            </label>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between text-xs text-muted-foreground">
              <span>蒙板表达式</span>
              <span className="text-xs">数值 ×255</span>
            </div>
            <textarea
              className="min-h-28 w-full resize-y rounded-md border border-border bg-[#0a1a30] p-2 font-mono text-xs leading-normal text-foreground outline-none focus:border-[#4a6a9a]"
              spellCheck={false}
              value={expression}
              onChange={(event) => setExpression(event.target.value)}
              placeholder='(当前蒙板-("白色背景"+"对话气泡"))'
            />
            <div className="flex gap-2">
              <Button size="sm" onClick={handleExpand}>
                分号转相加
              </Button>
              <Button size="sm" onClick={handleFormat}>
                缩进格式化
              </Button>
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">
              运算符 + - * / min / max / blur / 比较；优先级 * &gt; + - &gt; 比较。支持多行，
              「分号转相加」展开概念列表，「缩进格式化」重排排版。
            </p>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Checkbox checked={batch} onCheckedChange={setBatch} />
            批量处理整个目录
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <Checkbox
              checked={skipExisting}
              disabled={!batch}
              onCheckedChange={setSkipExisting}
            />
            跳过已有蒙版
          </label>
        </div>
      </div>
    </Dialog>
  );
}
