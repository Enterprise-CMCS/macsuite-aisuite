import os
import json
import boto3
import datetime
import time

from common.utils.aws_files_access import AwsFilesAccess
from common.utils.helper import Helper
from common.utils.logger import log
from common.utils.settings import aws_client,aws_session

from botocore.exceptions import ClientError


bedrock_runtime = None
bda_runtime_client = None
bda_client = None
s3AwsClient = None
session = None
current_region = None
sts_client = None
account_id = None


standard_output_configuration = {
    'document': {
        'extraction': {
            
            'granularity': {
                'types': [
                    'PAGE',     
                    'ELEMENT',  
                    'LINE',     
                ]
            },
            
            'boundingBox': {
                'state': 'ENABLED'
            }
        },
        
        'generativeField': {
            'state': 'ENABLED'
        },
        
        'outputFormat': {
            'textFormat': {
                'types': [
                    'PLAIN_TEXT',  
                    'MARKDOWN',    
                    'HTML',        
                    'CSV',         
                ]
            },
            
            'additionalFileFormat': {
                'state': 'ENABLED'
            }
        }
    },
    
    'image': {
        'extraction': {
            
            'category': {
                'state': 'ENABLED',
                'types': [
                    'TEXT_DETECTION',  
                ]
            },
            
            'boundingBox': {
                'state': 'ENABLED'
            }
        },
       
        'generativeField': {
            'state': 'ENABLED',
            'types': [
                'IMAGE_SUMMARY'  
            ]
        }
    }
}


def create_bda_project(projectname, project_description, project_stage):
    log.debug("***************** bedrock_BDA.create_bda_project() start *****************************")
    log.debug("create_bda_project() Passed Project Name= " + projectname + ",project description =" + project_description)
    if not bda_client:
        log.warn("create_bda_project() There is no connection to BDA. Variable bda_client is not set")
    else:
        log.info("create_bda_project() Success. There is connection to BDA. Variable bda_client is set")

    projects_list = bda_client.list_data_automation_projects(projectStageFilter=project_stage)["projects"]
    print("list_data_automation_projects project_list=" + str(projects_list))

    projects_existing = []
    for project in projects_list:
        if project["projectName"] == projectname:
            projects_existing.append(project)
    log.debug(f"list_data_automation_projects projects_existing={projects_existing}")

    log.debug(f"Checking if BDA project name is in BDA client list_data_automation_projects. If exists Delete it.")
    if len(projects_existing) > 0:
        log.info(f"Deleting existing project: {projects_existing[0]}")
        bda_client.delete_data_automation_project(projectArn=projects_existing[0]["projectArn"])
    else:
        log.debug(f"Skipping delete, as project does not exist")

    log.debug(f"Creating BDA - Bedrock Data Automation Project. Project Name={projectname}")
    response = bda_client.create_data_automation_project(
        projectName=projectname,
        projectDescription=project_description,
        projectStage=project_stage,
        standardOutputConfiguration=standard_output_configuration,
    )
    
    log.debug(f"Created a new project response= : {json.dumps(response)}")
    new_project_arn = response["projectArn"]
    log.info(f"New project {json.dumps(response)}, ARN is getting returned project_arn =  {new_project_arn}")

    return new_project_arn


def get_nested_value(data, path):
    log.debug("***************** bedrock_BDA.get_nested_value start *****************************")
    keys = path.split('.')
    for key in keys:
        if isinstance(data, dict) and key in data:
            data = data[key]
        else:
            return None
    return data


def wait_for_completion(client,get_status_function,status_kwargs,status_path_in_response,completion_states,error_states,max_iterations=60,delay=10):
    log.info("***************** bedrock_BDA.wait_for_completion start *****************************")
    for currentIterations in range(max_iterations):
        log.debug(f" Current Iteration={currentIterations}")
        try:
            response = get_status_function(**status_kwargs)
            status = get_nested_value(response, status_path_in_response)

            if status in completion_states:
                log.debug(f"Operation completed successfully with status: {status}")
                return response

            if status in error_states:
                log.error("Error", error=f"wait_for_completion() Operation failed with status: {status}")
                raise Exception(f"wait_for_completion() Operation failed with status: {status}")

            log.debug(f"Current status: {status}. Waiting...")
            time.sleep(delay) 

        except ClientError as lclException:
            print("Error Occurred")
            Helper.print_exception("wait_for_completion", lclException, extra_msg="Exception occurred")
            raise Exception(f"Exception Occurred Error checking status: {str(lclException)}")

    raise Exception(f"wait_for_completion() Operation timed out after {max_iterations} iterations")


def wait_for_job_to_complete(invocation_arn):
    log.debug("***************** bedrock_BDA.wait_for_job_to_complete start *****************************")
    get_status_response = wait_for_completion(
        client=bda_runtime_client,
        get_status_function=bda_runtime_client.get_data_automation_status,
        status_kwargs={'invocationArn': invocation_arn},
        completion_states=['Success'],
        error_states=['ClientError', 'ServiceError'],
        status_path_in_response='status',
        max_iterations=15,
        delay=30
    )
    log.info(f"***************** bedrock_BDA.wait_for_job_to_complete End ******** Returning get_status_response= {get_status_response}***************")
    return get_status_response


def bda_invoke(project_arn, input_bucket, p_files, output_bucket, out_prefix, da_profile_arn, project_stage):
    log.info("***************** bedrock_BDA.bda_invoke() start *****************************")
    log.info("bda_invoke() called with \n project_arn =" + project_arn + ", \n input_bucket=" + input_bucket + ", \n output_bucket=" + output_bucket + ", \n out_prefix=" + out_prefix + ", \n da_profile_arn=" + da_profile_arn + ",  \n ProjectStage=" + project_stage)

    # Delete All file in output bucket
    full_refresh = Helper.get_property("full_refresh")
    full_refresh= (full_refresh == "True")
    if full_refresh is True:
        log.debug(f"full_refresh = {full_refresh}, if true delete all object before updates ")
        s3_resource = boto3.resource('s3')
        Helper.delete_bucket_file_recursively(s3_resource, output_bucket, out_prefix)
    else:
        log.debug(f"full_refresh = {full_refresh}, if NOT True Dont delete all object before updates ")

    invoked = []

    for key in p_files:
        log.info(f"********************************** Key ={key} *********************** START ")
        str_basename = os.path.basename(key)
        log.debug("str_basename=" + str_basename + ", Setting rename file info variable ")
        rename_file_info = os.path.splitext(str_basename)[0]
        log.debug(f"rename_file_info={rename_file_info}, Setting rename file info variable. Replacing  empty string in name of file with _")
        rename_file_info = rename_file_info.replace(" ", "_")

        prefix_check = f"{out_prefix.rstrip('/')}/{rename_file_info}"
        log.info(f"strBasename={str_basename}, rename_file_info={rename_file_info}, Setting prefix_check variable. prefix_check={ prefix_check} ")
        found_metadata = False
        paginator = s3AwsClient.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(Bucket=output_bucket, Prefix=prefix_check):
                for obj in page.get("Contents", []):
                    if obj.get("Key", "").endswith("job_metadata.json"):
                        found_metadata = True
                        log.info(f'Checked if Key Ends with job_metadata.json, If found then skip {obj.get("Key", "")}')
                        break
                if found_metadata:
                    break
        except ClientError as lclEx:
            log.info(f"Warning: could not check outputs for {rename_file_info}: {lclEx}")
            raise Exception(f"bda_invoke() Warning: could not check outputs for {rename_file_info}: {lclEx}")

        if found_metadata:
            log.info(f"Checked if Key Ends with job_metadata.json, If found then Skipping (already processed): {rename_file_info}")
            continue

        input_s3_uri = f"s3://{input_bucket}/{key}"
        output_s3_uri = f"s3://{output_bucket}/{prefix_check}"

        log.info(f"Invoking BDA for: {rename_file_info}")
        log.debug(f"Input:  {input_s3_uri}")
        log.debug(f"Output: {output_s3_uri}")

        response = bda_runtime_client.invoke_data_automation_async(
                inputConfiguration={"s3Uri": input_s3_uri},
                outputConfiguration={"s3Uri": output_s3_uri},
                dataAutomationConfiguration={"dataAutomationProjectArn": project_arn, "stage": project_stage},
                dataAutomationProfileArn=da_profile_arn,
            )
        
        invocation_arn = response.get("invocationArn")
        if invocation_arn:
            log.debug(f"Waiting for job to complete for:{rename_file_info}")
            job_status = wait_for_job_to_complete(invocation_arn)
            log.info(f"Job completed for {rename_file_info}: final status={job_status.get('status')}")

        log.debug(f"********************************** Key ={key} *********************** End  ")
    log.debug(f"****************** bda_invoke() ******************************* End ****************************")
    return invoked


def invoke_bedrock_bda():
    log.info(f"******************Start of Function invokeBedrockBDA() of file{__file__}")
    try:

        global bedrock_runtime
        #bedrock_runtime = aws_client("bedrock-runtime")

        global bda_client
        bda_client = aws_client('bedrock-data-automation')
        if not bda_client:
            raise Exception (f"Bedrock Data Automation. Variable bda_client = {bda_client}, is not set properly. bedrock-data-automation client is not available")

        global bda_runtime_client
        bda_runtime_client= aws_client("bedrock-data-automation-runtime")
        if not bda_runtime_client:
            raise Exception (f"Bedrock Runtime Client. Variable bda_runtime_client = {bda_runtime_client}, is not set properly. BDA client is not available")

        global s3AwsClient
        s3AwsClient = aws_client("s3")
        if not s3AwsClient:
            raise Exception (f"S3 Client. Variable s3AwsClient = {s3AwsClient}, is not set properly. S3 client is not available")

        global session
        session = aws_session()
        if not session:
            raise Exception (f"AWS Session. Variable session = {session}, is not set properly. AWS Session is not available")

        global current_region
        current_region = session.region_name
        global sts_client
        sts_client = aws_client("sts")
        if not sts_client:
            raise Exception (f"Exception invokeBedrockBDA() AWS Client STS. Variable sts_client = {sts_client}, is not set properly. STS client is not available")
        else:
            log.debug(f"AWS Client STS. Variable sts_client = {sts_client}, is set properly.")

        global account_id
        account_id = sts_client.get_caller_identity()['Account']
        if account_id is None:
            raise Exception ("Account ID is Null. Variable account_id is not set properly. ")
        if current_region is None:
            raise Exception("Current region is Null. Variable current_region is not set properly. ")
        #Test Exception
        #a = 1 / 0

        log.info(f"sts_client={sts_client }, current_region={current_region }, account_id={account_id}")
    except Exception as Ex:
        Helper.print_exception("invoke_bedrock_bda", Ex, "Error in Getting Values for Global Variables.")
        return False
        #raise Ex

    try:

        project_name = Helper.get_property("projectname")
        project_description = Helper.get_property("projectdescription")
        input_bucket_name = Helper.get_property("input_bucket_name")
        input_prefix = Helper.get_property("input_prefix")
        output_bucket = Helper.get_property("output_bucket")
        output_prefix = Helper.get_property("output_prefix")
        project_stage = Helper.get_property("ProjectStage")
        data_automation_v = Helper.get_property("DataAutomationV")

        log.info(
            f"projectname ={project_name} projectdescription={project_description }, input_bucket_name={ input_bucket_name} input_prefix={ input_prefix}, output_bucket={ output_bucket},  output_prefix={output_prefix}")
        log.debug(f"ProjectStage ={project_stage},  data_automation_v={ data_automation_v}")

        project_arn = create_bda_project(project_name, project_description, project_stage)

        log.debug("Calling AwsFilesAccess")
        aws_files_access = AwsFilesAccess(s3AwsClient)
        log.debug(f"After Calling AwsFilesAccess. Now calling aws_file_lists")
        data_files = aws_files_access.aws_file_lists(input_bucket_name, input_prefix)

        now = datetime.datetime.now()
        Helper.summary_file("BEDROCK",
                           f"\n{now} Number of PDF or DOCX files that are present is {len(data_files)} in bucket {input_bucket_name} at prefix {input_prefix}")
        Helper.summary_file("BEDROCK", f"\n{now} File Names are " + str(data_files))
        log.info(
            f"Number of PDF or DOCX files that are present is {len(data_files)} in bucket {input_bucket_name} at prefix {input_prefix}")
        log.debug("File Names are " + str(data_files))

        da_profile_arn = f'arn:aws:bedrock:{current_region}:{account_id}:data-automation-profile/{data_automation_v}'
        Helper.summary_file("BEDROCK", f"\n{now} Formatted da_profile_arn=" + str(da_profile_arn))
        print(f"Formatted da_profile_arn={da_profile_arn}")
        bda_results = bda_invoke(project_arn, input_bucket_name, data_files, output_bucket, output_prefix, da_profile_arn,
                                 project_stage)
        log.info("***************** invokeParsedBDATable End. __name__=" + str(__name__))
        return True

    except Exception as ex:  # Catches any exception and stores it in 'lclAllEx'
        Helper.print_exception("invokeBedrockBDA", ex)
        # Re-raise the same exception
        log.info(f"***************** invokeParsedBDATable End. __name__={__name__}. Returning False")
        return False
