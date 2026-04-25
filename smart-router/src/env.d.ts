/**
 * Smart Router - Environment type declarations
 */

declare interface Env {
  HEALTH_TRACKER: DurableObjectNamespace;
  DB: D1Database;
  KEY_ENCRYPTION_KEY: string;
}

declare module "*.json" {
  const value: unknown;
  export default value;
}
