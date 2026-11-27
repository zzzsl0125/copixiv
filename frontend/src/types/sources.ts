/** Source domain types — thin aliases over the generated OpenAPI schemas. */
import type { components } from './api.generated'

type S = components['schemas']

export type SourceItem = S['SourceItemResponse']
export type SourceManifest = S['SourceManifestSchema']
export type SettingFieldSpec = S['SettingFieldSpecSchema']
export type CredentialKindSpec = S['CredentialKindSpecSchema']
export type CredentialFieldSpec = S['CredentialFieldSpecSchema']
export type SourceShelfDef = S['SourceShelfDefSchema']
export type SourceConfigUpdate = S['SourceConfigUpdateRequest']
export type SourceTestResult = S['SourceTestResponse']
export type SourceCredential = S['SourceCredentialResponse']
export type SourceCredentialCreate = S['SourceCredentialCreateRequest']
export type SourceCredentialUpdate = S['SourceCredentialUpdateRequest']
export type SourceFetchRequest = S['SourceFetchRequest']
export type SourceFetchResponse = S['SourceFetchResponse']
