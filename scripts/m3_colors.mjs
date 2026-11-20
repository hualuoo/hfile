// The color tokens at the top of apps/web/static/web/css/m3.css: Material 3 color roles generated with
// Google's material-color-utilities from HFile's seed color. Prints the CSS; paste it over the color
// blocks of m3.css (between the "generated" comments).
//
//   mkdir /tmp/m3 && cd /tmp/m3 && npm install @material/material-color-utilities@0.4.0
//   NODE_PATH=/tmp/m3/node_modules node scripts/m3_colors.mjs [seed] [success] [warning]
//
// The recipe (the same as rolehomes'): the M3 baseline scheme's palettes (CorePalette.of), for a source
// of hue H and chroma C: primary (H, max(48, C)), secondary (H, 16), tertiary (H + 60, 24),
// neutral (H, 4), neutral variant (H, 8); the roles from the 2021 dynamic color spec, contrast 0.
// "success" (harmonized toward the seed) and "warning" (not harmonized, so it stays amber rather
// than drifting toward the error red) are custom colors made the way Material Theme Builder makes
// them: tones 40 / 100 / 90 / 10 in light, 80 / 20 / 30 / 90 in dark.
import { createRequire, register } from 'node:module';
import { pathToFileURL } from 'node:url';

// 0.4.0's ES modules import one file without its .js (dynamiccolor/color_spec_2025.js), which Node
// refuses: a resolve hook tries again with .js.
register('data:text/javascript,' + encodeURIComponent(`
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context);
  } catch (error) {
    if (error.code === 'ERR_MODULE_NOT_FOUND' && specifier.startsWith('.') && !specifier.endsWith('.js')) {
      return next(specifier + '.js', context);
    }
    throw error;
  }
}`));
const require = createRequire(import.meta.url);
const m3 = await import(pathToFileURL(require.resolve('@material/material-color-utilities')).href);
const { DynamicScheme, Hct, MaterialDynamicColors, TonalPalette, Variant, argbFromHex, hexFromArgb, customColor } = m3;

const [seed = '#1E6FD9', success = '#2e7d32', warning = '#b86e00'] = process.argv.slice(2);

function scheme(isDark) {
  const source = Hct.fromInt(argbFromHex(seed));
  const palette = (hue, chroma) => TonalPalette.fromHueAndChroma(hue, chroma);
  return new DynamicScheme({
    sourceColorHct: source,
    variant: Variant.TONAL_SPOT,
    contrastLevel: 0,
    isDark,
    specVersion: '2021',
    primaryPalette: palette(source.hue, Math.max(48, source.chroma)),
    secondaryPalette: palette(source.hue, 16),
    tertiaryPalette: palette((source.hue + 60) % 360, 24),
    neutralPalette: palette(source.hue, 4),
    neutralVariantPalette: palette(source.hue, 8),
  });
}

// The roles m3.css defines, in its order.
const ROLES = [
  'primary', 'onPrimary', 'primaryContainer', 'onPrimaryContainer', 'inversePrimary',
  'secondary', 'onSecondary', 'secondaryContainer', 'onSecondaryContainer',
  'tertiary', 'onTertiary', 'tertiaryContainer', 'onTertiaryContainer',
  'error', 'onError', 'errorContainer', 'onErrorContainer',
  'surface', 'onSurface', 'surfaceVariant', 'onSurfaceVariant', 'surfaceDim', 'surfaceBright',
  'surfaceContainerLowest', 'surfaceContainerLow', 'surfaceContainer', 'surfaceContainerHigh', 'surfaceContainerHighest',
  'inverseSurface', 'inverseOnSurface', 'outline', 'outlineVariant', 'shadow', 'scrim',
  'primaryFixed', 'primaryFixedDim', 'onPrimaryFixed', 'onPrimaryFixedVariant',
];
const kebab = (name) => name.replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`);
const hex = (argb) => hexFromArgb(argb).toLowerCase();

function block(isDark, indent) {
  const s = scheme(isDark);
  const colors = new MaterialDynamicColors();
  const lines = ROLES.map((role) => `--md-sys-color-${kebab(role)}: ${hex(colors[role]().getArgb(s))};`);
  lines.push('/* custom colors: see the top of this file */');
  for (const [name, value, blend] of [['success', success, true], ['warning', warning, false]]) {
    const c = customColor(argbFromHex(seed), { name, value: argbFromHex(value), blend })[isDark ? 'dark' : 'light'];
    lines.push(`--md-sys-color-${name}: ${hex(c.color)};`, `--md-sys-color-on-${name}: ${hex(c.onColor)};`,
      `--md-sys-color-${name}-container: ${hex(c.colorContainer)};`, `--md-sys-color-on-${name}-container: ${hex(c.onColorContainer)};`);
  }
  return lines.map((line) => indent + line).join('\n');
}

console.log(`/* generated: scripts/m3_colors.mjs ${seed} ${success} ${warning} */`);
console.log(':root, [data-theme="light"] {\n  color-scheme: light;\n' + block(false, '  ') + '\n}');
console.log('@media (prefers-color-scheme: dark) {\n  :root:not([data-theme="light"]) {\n    color-scheme: dark;\n' + block(true, '    ') + '\n  }\n}');
console.log('[data-theme="dark"] {\n  color-scheme: dark;\n' + block(true, '  ') + '\n}');
console.log('/* end of generated */');
