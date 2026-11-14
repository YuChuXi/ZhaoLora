/** 蒙版模板管理对话框：合并方式、框选捕获与 10 个槽位卡片. */

import type { MaskTemplate } from './types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { cn } from '@/lib/utils';

/** 合并方式. */
export type MergeMode = 'min' | 'max';

/** 模板管理对话框属性. */
interface TemplateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  templates: Array<MaskTemplate | null>;
  mergeMode: MergeMode;
  onMergeMode: (mode: MergeMode) => void;
  onDelete: (slot: number) => void;
  onCapture: () => void;
}

/**
 * 槽位索引映射到键盘键位.
 *
 * Args:
 *   index (number): 槽位索引 0-9.
 *
 * Returns:
 *   string: 0-8 为 '1'-'9'，9 为 '0'.
 *
 * Callers:
 *   - `frontend/src/pages/paint/TemplateDialog.tsx:TemplateDialog`
 *   - `frontend/src/pages/paint/PaintPage.tsx` 快捷键与提示.
 */
export function slotKey(index: number): string {
  return index === 9 ? '0' : String(index + 1);
}

/**
 * 模板管理对话框：展示槽位图案并触发捕获/删除.
 *
 * Args:
 *   props (TemplateDialogProps): 槽位、合并方式与操作回调.
 *
 * Returns:
 *   JSX.Element: 模态对话框.
 *
 * Callers:
 *   - `frontend/src/pages/paint/PaintPage.tsx:PaintPage`
 */
export function TemplateDialog({
  open,
  onOpenChange,
  templates,
  mergeMode,
  onMergeMode,
  onDelete,
  onCapture,
}: TemplateDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="蒙板模板"
      className="w-[min(720px,92vw)]"
      footer={<Button onClick={() => onOpenChange(false)}>关闭</Button>}
    >
      <div className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-4">
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="radio"
              name="merge-mode"
              checked={mergeMode === 'min'}
              onChange={() => onMergeMode('min')}
            />
            min 合并
          </label>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <input
              type="radio"
              name="merge-mode"
              checked={mergeMode === 'max'}
              onChange={() => onMergeMode('max')}
            />
            max 合并
          </label>
          <span className="text-[11px] text-muted-foreground">与当前蒙板按 min（默认）/ max 合并</span>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button variant="primary" onClick={onCapture}>
            框选当前图区域保存为模板
          </Button>
          <span className="text-[11px] text-muted-foreground">
            放置: 1-9/0，拖动/缩放（Shift 以中心为锚点），单击加锚点（≤2），拖锚点移动，
            空格/Enter 应用，Esc 取消
          </span>
        </div>

        <div className="grid grid-cols-5 gap-2">
          {templates.map((template, slot) => (
            <div
              key={slot}
              className={cn(
                'relative rounded-md border border-border p-1.5 text-center',
                template ? '' : 'opacity-40',
              )}
            >
              <div className="text-[11px] text-muted-foreground">键 {slotKey(slot)}</div>
              {template ? (
                <>
                  <img
                    src={template.url}
                    alt={`模板 ${slotKey(slot)}`}
                    className="my-1 block h-[52px] w-full object-contain"
                    style={{
                      background:
                        'repeating-conic-gradient(#333 0 25%, #222 0 50%) 0 0 / 10px 10px',
                    }}
                  />
                  <button
                    type="button"
                    className="absolute right-1 top-0.5 text-xs leading-none text-muted-foreground hover:text-foreground"
                    title="删除模板"
                    onClick={() => onDelete(slot)}
                  >
                    ×
                  </button>
                </>
              ) : (
                <div className="h-[52px]" />
              )}
            </div>
          ))}
        </div>
      </div>
    </Dialog>
  );
}
