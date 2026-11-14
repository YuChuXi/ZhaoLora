import { forwardRef, type ButtonHTMLAttributes } from 'react';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

const buttonVariants = cva(
  'inline-flex items-center justify-center gap-1 whitespace-nowrap rounded-md border text-xs font-medium transition-colors disabled:pointer-events-none disabled:opacity-40',
  {
    variants: {
      variant: {
        default: 'border-border bg-secondary text-secondary-foreground hover:border-[#4a6a9a] hover:bg-accent',
        primary: 'border-[#2a5a3a] bg-[#1a3a2a] text-green-300 hover:bg-[#2a5a3a]',
        danger: 'border-primary bg-primary text-primary-foreground hover:bg-[#ff6b81]',
        ghost: 'border-transparent bg-transparent text-muted-foreground hover:text-foreground',
      },
      size: {
        default: 'h-7 px-3',
        sm: 'h-6 px-2 text-[11px]',
        icon: 'h-7 w-7 p-0',
      },
    },
    defaultVariants: { variant: 'default', size: 'default' },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => (
    <button ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props} />
  ),
);
Button.displayName = 'Button';
