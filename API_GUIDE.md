# Homebox API Guide for LLMs

This document provides guidance on how to interface with the Homebox API system. The information is structured to help language models effectively understand and interact with the API.

## API Structure Overview

- **Base URL**: All API endpoints are prefixed with `/api/v1/`
- **Authentication**: Bearer token authentication
- **Documentation**: Swagger/OpenAPI 2.0 available at `/swagger/*` endpoints

## Authentication Flow

1. **Register**: `POST /api/v1/users/register` (if allowed by server)
2. **Login**: `POST /api/v1/users/login` with email/password to get access token
3. **Authentication**: Include token in `Authorization: Bearer <token>` header
4. **Token Refresh**: `GET /api/v1/users/refresh` to get a new token
5. **Logout**: `POST /api/v1/users/logout` to invalidate token

Alternative authentication methods:
- Query parameter: `?access_token=<token>`
- Cookie-based: `hb.auth.session` cookie

## Core Resources

### Items
- `GET /api/v1/items` - List all items (supports pagination, filtering)
- `POST /api/v1/items` - Create a new item
- `GET /api/v1/items/{id}` - Get a specific item
- `PUT /api/v1/items/{id}` - Update an item
- `DELETE /api/v1/items/{id}` - Delete an item
- `POST /api/v1/items/import` - Import items (CSV/JSON)
- `GET /api/v1/items/export` - Export items

### Locations
- `GET /api/v1/locations` - List all locations
- `POST /api/v1/locations` - Create a location
- `GET /api/v1/locations/{id}` - Get a location
- `PUT /api/v1/locations/{id}` - Update a location
- `DELETE /api/v1/locations/{id}` - Delete a location
- `GET /api/v1/locations/tree` - Get locations as a tree structure

### Labels
- `GET /api/v1/labels` - List all labels
- `POST /api/v1/labels` - Create a label
- `GET /api/v1/labels/{id}` - Get a label
- `PUT /api/v1/labels/{id}` - Update a label
- `DELETE /api/v1/labels/{id}` - Delete a label

### Attachments
- `POST /api/v1/items/{id}/attachments` - Add attachment to item
- `GET /api/v1/items/{id}/attachments/{attachment_id}` - Get attachment
- `PUT /api/v1/items/{id}/attachments/{attachment_id}` - Update attachment
- `DELETE /api/v1/items/{id}/attachments/{attachment_id}` - Delete attachment

## Request/Response Formats

### Common Properties
- All resources typically have: `id`, `createdAt`, `updatedAt`
- Timestamps use ISO 8601 format
- Zero dates represented as: `0001-01-01T00:00:00Z`

### Pagination
Query parameters for list endpoints:
- `page`: Page number (starting at 1)
- `pageSize`: Number of items per page

### Filtering
Most collection endpoints support filtering:
- `orderBy`: Field to sort by
- `q`: Search query
- Resource-specific filters (e.g., `locations`, `labels` for items)

## Example API Usage

### Authentication
```javascript
// Login
const response = await fetch('/api/v1/users/login', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ email: 'user@example.com', password: 'password' })
});
const { token } = await response.json();

// Using the token
const headers = { 'Authorization': `Bearer ${token}` };
```

### Create Item
```javascript
const item = {
  name: "New Item",
  description: "Description of the item",
  labelIds: ["label-id-1", "label-id-2"],
  locationId: "location-id",
  fields: {
    "Purchase Price": "100.00",
    "Warranty": "2 years"
  }
};

await fetch('/api/v1/items', {
  method: 'POST',
  headers: { 
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${token}`
  },
  body: JSON.stringify(item)
});
```

### Upload Attachment
```javascript
const formData = new FormData();
formData.append('file', fileBlob);
formData.append('type', 'photo'); // Or 'warranty', 'manual', 'receipt'
formData.append('name', 'filename.jpg');

await fetch(`/api/v1/items/${itemId}/attachments`, {
  method: 'POST',
  headers: { 'Authorization': `Bearer ${token}` },
  body: formData
});
```

## Error Handling

- Successful responses return HTTP 2xx status codes
- Error responses return HTTP 4xx or 5xx status codes
- Error details are provided in JSON format
- Authentication errors return HTTP 401

## Development Notes

- The API follows RESTful principles
- Date handling requires special attention due to multiple formats
- File uploads use multipart/form-data format
- Fields with null values may be omitted from responses