/** Token domain types */

export interface Token {
  id: number
  name: string
  token: string
  premium: boolean
  valid: boolean
  is_follow: boolean
}

export interface TokenCreate {
  name: string
  token: string
  premium?: boolean
  valid?: boolean
}

export interface TokenUpdate {
  name?: string
  token?: string
  premium?: boolean
  valid?: boolean
  is_follow?: boolean
}
