import type { components } from '../api/schema'

export type Persona = components['schemas']['DemoLogin']['persona']
export type Session = components['schemas']['SessionView']
export type Result = components['schemas']['ReportInput']['result']
export type Photo = components['schemas']['PhotoView']
export type Ticket = components['schemas']['TicketView']
export type Catalog = components['schemas']['app__schemas__CatalogView']
export type Assignment = components['schemas']['WorkerAssignmentView']
export type Assigned = components['schemas']['AssignView']
