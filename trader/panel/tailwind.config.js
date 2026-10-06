/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Deep navy ground. Each step is one elevation level, not a decoration.
        ink: {
          950: '#060912',
          900: '#0c1222',
          850: '#111a2e',
          800: '#172136',
          700: '#22304a',
          600: '#34466a',
        },
        // Brightened one step versus the usual Tailwind 500s so they survive sunlight.
        profit: { DEFAULT: '#34d399', deep: '#059669' },
        loss: { DEFAULT: '#fb7185', deep: '#e11d48' },
        accent: { DEFAULT: '#ffc700', deep: '#e0a800' },
        warn: { DEFAULT: '#fb923c', deep: '#ea580c' },
      },
      fontFamily: {
        sans: ['Vazirmatn', 'system-ui', 'Segoe UI', 'Tahoma', 'sans-serif'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem' }],
        hero: ['2.5rem', { lineHeight: '1.05', letterSpacing: '-0.02em' }],
      },
      borderRadius: {
        '2.5xl': '1.25rem',
      },
      spacing: {
        tabbar: '4.25rem',
        header: '3.5rem',
      },
      boxShadow: {
        sheet: '0 -16px 48px -16px rgba(0,0,0,.75)',
        modal: '0 24px 64px -16px rgba(0,0,0,.8)',
      },
      keyframes: {
        breathe: {
          '0%, 100%': { boxShadow: '0 0 0 0 rgba(52,211,153,.55)' },
          '50%': { boxShadow: '0 0 0 6px rgba(52,211,153,0)' },
        },
        'sheet-up': {
          from: { transform: 'translateY(100%)' },
          to: { transform: 'translateY(0)' },
        },
        'pop-in': {
          from: { opacity: '0', transform: 'translateY(8px) scale(.98)' },
          to: { opacity: '1', transform: 'translateY(0) scale(1)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to: { opacity: '1' },
        },
      },
      animation: {
        breathe: 'breathe 2.4s ease-out infinite',
        'sheet-up': 'sheet-up .28s cubic-bezier(.2,.8,.2,1)',
        'pop-in': 'pop-in .2s ease-out',
        'fade-in': 'fade-in .2s ease-out',
      },
    },
  },
  plugins: [],
}
