import type { UserConfig } from 'tsdown'

const PLUGIN_ID = '@sgme/sgme'

const config: UserConfig = {
  name: PLUGIN_ID,
  entry: ['src/index.ts'],
  outDir: 'lib',
  format: ['esm'],
  platform: 'node',
  target: 'es2024',
  fixedExtension: false,
  dts: false,
  clean: true,
  deps: { neverBundle: ['cordis', 'schemastery'] },
}

export default config
