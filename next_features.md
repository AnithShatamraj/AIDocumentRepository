Now I want to add a funtionality to for the previlaged users to define their own document types with a defined set of metadata.
So the user can define a new document type.
Document Type:
Name:
Description:
Metadata (Fields):
	Name:
	Description:
	Datatype: [Number, String, Date, Time, DateTime, Object(Group of Fields), List[<datatype>]]
				If object: it can have nested fields.
				If list: it can be list of any datatype including Object.


User can upload a doucment either by selecting a doucment type or by auto detect option. User can also provide a name of his choice for the document, however check for the document, check for the uniqueness for the name if the name already exists then ask him to provide another name.
When uploaded check for the uniqueness of the content by hashing the document, if exact same document is available then:
 - either provide the user access to this document and notify him that this exists and notify all other users that this user has uploaded this document and access is provided to him.
 - else atleast we can make the document mapping so that we skip all the preprocessing steps since it is not required and would save cost.
 - We can may be make this feature configurable at the admin level "allow duplicate documents".

When the document type is autodetected all the metadata listed must be extracted ustilizing the metadata name and description provided, also continue with existing flow for finding additional metadata.

For the list type more than one value can be extracted. e.g. party name can be more than one, or for list of objects: in resumes user can have multiple work experience with each work experience having attributes like oranization name, start date, end date, designation, roles and responsibilities etc.

Give me a plan to achieve this
- what changes are required in the DB schema changes and how would you handle lists and nested attributes.
- How would you store the metadata of the metadata. Sould we even call the current metadata, metadata or should we call it entities or any other name?
- How would you render the nested objects on the UI?