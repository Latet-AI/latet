// The build empties src/latet/web/; the repository tracks that folder through a .gitkeep file.
import { existsSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const web = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'src', 'latet', 'web')
if (!existsSync(join(web, 'index.html'))) {
  console.error(`keep-gitkeep: ${web} has no index.html; did the build run?`)
  process.exit(1)
}
writeFileSync(join(web, '.gitkeep'), '')
console.log(`keep-gitkeep: ${join(web, '.gitkeep')}`)
