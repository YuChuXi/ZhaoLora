import * as CheckboxPrimitive from '@radix-ui/react-checkbox';
import * as SliderPrimitive from '@radix-ui/react-slider';
import { Check } from 'lucide-react';
import { cn } from '@/lib/utils';

export function Checkbox({
  checked,
  onCheckedChange,
  disabled,
  className,
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <CheckboxPrimitive.Root
      checked={checked}
      disabled={disabled}
      onCheckedChange={(v) => onCheckedChange(v === true)}
      className={cn(
        'flex h-3.5 w-3.5 items-center justify-center rounded-sm border border-border bg-[#0a1a30] data-[state=checked]:border-primary data-[state=checked]:bg-primary',
        className,
      )}
    >
      <CheckboxPrimitive.Indicator>
        <Check className="h-3 w-3 text-white" />
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}

export function Slider({
  value,
  min,
  max,
  step,
  onChange,
  onCommit,
}: {
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (value: number) => void;
  onCommit?: () => void;
}) {
  return (
    <SliderPrimitive.Root
      className="relative flex h-4 w-[90px] touch-none items-center"
      value={[value]}
      min={min}
      max={max}
      step={step ?? 1}
      onValueChange={(v) => onChange(v[0])}
      onValueCommit={() => onCommit?.()}
    >
      <SliderPrimitive.Track className="relative h-1 w-full grow rounded-full bg-border">
        <SliderPrimitive.Range className="absolute h-full rounded-full bg-primary" />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className="block h-3.5 w-3.5 rounded-full border border-primary bg-white shadow" />
    </SliderPrimitive.Root>
  );
}
