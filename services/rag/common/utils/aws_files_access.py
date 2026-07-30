from botocore.exceptions import ClientError
from common.utils.helper import Helper
from common.utils.logger import log


class AwsFilesAccess:
    def __init__(self,s3awsclient):
        self.s3AwsClient = s3awsclient

    def aws_file_lists(self, lcl_bucket_name, prefix):
        log.info(f"***************** Aws_files_Access Function aws_file_lists starts  ********** with passed \nlclBucketName={lcl_bucket_name}, prefix={prefix}")
        bucket_files = []

        if not self.s3AwsClient:
            log.info(f"aws_file_lists() Could not access the s3 for docx and pdf. BucketName={lcl_bucket_name}, prefix={prefix}")

        log.debug(f"aws_file_lists() Accessing S3 for docx and pdf. BucketName={lcl_bucket_name}, prefix={prefix}")
        try:
            paginator = self.s3AwsClient.get_paginator('list_objects_v2')
            log.debug("aws_file_lists() Adding file name list ")
            for page in paginator.paginate(Bucket=lcl_bucket_name, Prefix=prefix):
                log.debug(f"aws_file_lists() Adding file name list ")
                for obj in page.get("Contents",[]):
                    key = obj["Key"]
                    log.debug(f"aws_file_lists() Adding file name list {key} ")
                    if key.lower().endswith((".docx", ".pdf")):
                        log.info(f"aws_file_lists() Adding file name list {key} ")
                        bucket_files.append(key)
            log.info(f"aws_file_lists() Number of files that are present {len(bucket_files)}")
            log.debug("aws_file_lists() Returning file names which end with docs and pdf. ")
            return bucket_files

        except ClientError as lclEx:
            Helper.print_exception("aws_file_lists", lclEx, f"Error while retrieving the Bucket file Bucket Name = {lcl_bucket_name}\{prefix}.")
            raise lclEx

    def aws_excel_file(self, lcl_bucket_name, prefix):
        log.debug(f"************* Aws_files_Access Function aws_excel_file starts  ********** with passed \nlclBucketName={lcl_bucket_name}, \nprefix= {prefix}")
        bucket_files = []

        if not self.s3AwsClient:
            log.error("aws_excel_file() Could not access the s3 for xlsx files")

        log.info(f"aws_excel_file() Accessing S3 for xlsx files from lclBucketName  {lcl_bucket_name}  Prefix= {prefix} Get Content object and see if Content Object Key ends with .XLSX, if ends then and key name to Array and return to calling function.")
        try:
            paginator = self.s3AwsClient.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=lcl_bucket_name, Prefix=prefix):
                for obj in page.get("Contents",[]):
                    key = obj["Key"]
                    log.debug(f" aws_excel_file Object Key: {obj['Key']}")
                    log.debug(f"aws_excel_file() Value of Key={key}")
                    if key.lower().endswith(".xlsx"):
                        #log.info(f" aws_excel_file Object Looking for just .xlsx files Key: {obj['Key']}")
                        log.info(f"aws_excel_file() Looking for just .xlsx files Added file to array - {key}")
                        bucket_files.append(key)
            log.info(f"Number of files that are present {len(bucket_files)}")

            return bucket_files

        except Exception as lclEx:
            Helper.print_exception("aws_excel_file", lclEx, f"Error while retrieving the Bucket file Bucket Name = {lcl_bucket_name}\{prefix}.")
            raise lclEx

    def aws_file_with_extension(self, lcl_bucket_name, p_prefix, file_extension):
        log.debug(f"***************** Aws_files_Access Function aws_file_with_extension starts  ********** with passed \nlclBucketName={lcl_bucket_name}, prefix{p_prefix}, fileExtension={file_extension}")
        bucket_files = []

        if not self.s3AwsClient:
            log.info(f"aws_file_with_extension() Could not access the s3 for {file_extension}.  BucketName={lcl_bucket_name}, prefix={p_prefix}")

        log.info(f"aws_file_with_extension() Accessing S3 for {file_extension}. .  BucketName={lcl_bucket_name}, prefix={p_prefix}")
        try:
            paginator = self.s3AwsClient.get_paginator('list_objects_v2')
            log.debug(f"aws_file_with_extension() Adding file name list.")
            for page in paginator.paginate(Bucket=lcl_bucket_name,Prefix=p_prefix):

                for obj in page.get("Contents",[]):
                    key = obj["Key"]
                    log.debug(f"aws_file_with_extension() Adding file name list if ends with {file_extension} for file {key} ")
                    if key.lower().endswith(file_extension):
                        log.info(f"aws_file_lists() Added file with extension{file_extension} name list {key} ")
                        bucket_files.append(key)
            log.info(f"aws_file_with_extension() Number of files that are present {len(bucket_files)} with extension{file_extension}")
            log.debug(f"aws_file_with_extension() Returning file names which end with {file_extension}. ")
            return bucket_files

        except ClientError as lclEx:
            Helper.print_exception(f"aws_file_with_extension() ", lclEx , f"Error while retrieving file wirh extension {file_extension} from Bucket Name = {lcl_bucket_name}, prefix={p_prefix}.")
            raise lclEx

    def aws_upload_to_s3(self, lcl_p_bucket_name, prefix):
        pass
               




