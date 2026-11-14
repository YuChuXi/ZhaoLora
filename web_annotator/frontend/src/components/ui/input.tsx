import { forwardRef, type InputHTMLAttributes, type SelectHTMLAttributes } from 'react';
import { cn } from '@/lib/utils';

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  ({ className, ...props }, ref) => (
    <input
      ref={ref}
      className={cn(
        'h-7 rounded-md border border-border bg-[#0a1a30] px-2 text-xs text-foreground outline-none focus:border-[#4a6a9a] disabled:opacity-40',
        className,
      )}
      {...props}
    />
  ),
);
Input.displayName = 'Input';

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  ({ className, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        'h-7 rounded-md border border-border bg-[#0a1a30] px-2 text-xs text-foreground outline-none focus:border-[#4a6a9a] disabled:opacity-40',
        className,
      )}
      {...props}
    />
  ),
);
Select.displayName = 'Select';
