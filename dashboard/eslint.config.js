import js from '@eslint/js'
import i18next from 'eslint-plugin-i18next'
import reactHooks from 'eslint-plugin-react-hooks'
import { defineConfig, globalIgnores } from 'eslint/config'
import globals from 'globals'
import tseslint from 'typescript-eslint'

// Attributes that never reach the user as text. Everything else in JSX (text, aria-label, title,
// placeholder, alt...) must come from a translation.
const NOT_TEXT_ATTRIBUTES = [
  'className',
  'style',
  'type',
  'key',
  'id',
  'width',
  'height',
  'role',
  'to',
  'href',
  'htmlFor',
  'name',
  'value',
  'd',
  'viewBox',
  'fill',
  'stroke',
  'strokeLinecap',
  'strokeLinejoin',
  'textAnchor',
  'dy',
  'shapeRendering',
  'transform',
  'data-tone',
  'data-href',
  'data-pair',
  'data-tutorial-pulse',
  'autoComplete',
  'inputMode',
  'rel',
  'target',
  'list',
  'aria-hidden',
  'aria-current',
  'aria-live',
  'focusable',
  'dir',
  'lang',
  // Props of this app's components that take identifiers, not text.
  'icon',
  'status',
  'color',
  'variant',
  'scope',
  'size',
  'placement',
  'tone',
  'side',
  'mode',
  'cls',
  'layer',
  // A section of the documentation, by its name in lib/docsLinks.json.
  'docs',
]

// Calls whose string arguments are identifiers (state keys, CSS classes, separators), not text.
const NOT_TEXT_CALLEES = [
  't',
  'i18n(ext)?',
  'tk',
  'require',
  'addEventListener',
  'removeEventListener',
  'set',
  'setParam',
  'setFilter',
  'push',
  'join',
  'includes',
  'startsWith',
  'endsWith',
  'navigate',
]

export default defineConfig([
  globalIgnores(['node_modules', 'dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, tseslint.configs.recommended, reactHooks.configs.flat['recommended-latest']],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
    },
    rules: {
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_', ignoreRestSiblings: true }],
    },
  },
  {
    files: ['src/**/*.tsx'],
    plugins: { i18next },
    rules: {
      'i18next/no-literal-string': [
        'error',
        {
          mode: 'jsx-only',
          'jsx-attributes': { exclude: NOT_TEXT_ATTRIBUTES },
          'jsx-components': { exclude: ['Trans'] },
          callees: { exclude: NOT_TEXT_CALLEES },
          // Option objects ({ value: 'new', label: t(...) }): the value is an identifier.
          'object-properties': { exclude: ['value', 'key', 'icon', 'tone', 'id'] },
          words: {
            // Strings without letters (punctuation, numbers, symbols) are not text.
            exclude: [/^[\s\p{P}\p{S}\p{N}]*$/u],
          },
        },
      ],
    },
  },
  {
    files: ['*.config.{ts,js}', 'scripts/**/*.mjs'],
    languageOptions: { globals: globals.node },
  },
  {
    files: ['scripts/**/*.mjs'],
    extends: [js.configs.recommended],
    languageOptions: { globals: globals.node, ecmaVersion: 2023, sourceType: 'module' },
  },
])
