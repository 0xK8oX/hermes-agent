/**
 * Smart Router - Environment type declarations
 */

declare interface Env {
  HEALTH_TRACKER: DurableObjectNamespace;
  PLAN_STORE: DurableObjectNamespace;
}

declare module "*.json" {
  const value: unknown;
  export default value;
}
