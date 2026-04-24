/**
 * Smart Router - Environment type declarations
 */

declare interface Env {
  HEALTH_TRACKER: DurableObjectNamespace;
}

declare module "*.json" {
  const value: unknown;
  export default value;
}
