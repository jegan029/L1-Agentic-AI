// Single source for identity strings and assets. Nothing here names a customer:
// white-label a deployment by setting the VITE_BRAND_* variables at build time
// (see frontend/.env.example). Per-tenant branding arrives with tenancy.
const env = import.meta.env

export const brand = {
  product: env.VITE_BRAND_PRODUCT || 'L1 and L2 AI-Ops',
  org: env.VITE_BRAND_ORG || '',
  orgBanner: env.VITE_BRAND_BANNER || '',
  logo: env.VITE_BRAND_LOGO || '',
  footer: env.VITE_BRAND_FOOTER || 'L1 and L2 AI-Ops',
}
