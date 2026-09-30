// Node's URLPattern typings are not bundled by all supported @types/node releases.
// Next.js only needs these names for its server-side declaration surface.
interface URLPatternOptions { ignoreCase?: boolean }
type URLPatternInput = string | URLPatternInit
