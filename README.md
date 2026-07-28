# docker

一键安装命令

```bash
bash <(curl -sSL "https://raw.githubusercontent.com/warptr/docker/main/mp-setup-choose.sh")
```

一键安装浏览器核心命令

```bash
bash <(curl -sSL "https://raw.githubusercontent.com/warptr/docker/main/update_mp_core.sh")

bash <(curl -sSL "https://raw.githubusercontent.com/warptr/docker/main/update_mp_core.sh" | tr -d '\r')
```



各分支同步main命令

```
$branches = "dsm", "fnos", "fnos-arm", "qnap", "ugreen", "zspace", "zspace-arm", "zspace-z4s"; foreach ($b in $branches) { git checkout $b; git merge main --no-edit; git push } ; git checkout main
```



## Getting started

To make it easy for you to get started with GitHub, here's a list of recommended next steps.

Already a pro? Just edit this README.md and make it your own.

## Add your files

* [Create](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-files-to-a-repository) or [upload](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-files-to-a-repository#uploading-files) files
* [Add files using the command line](https://docs.github.com/en/repositories/working-with-files/managing-files/adding-files-to-a-repository) or push an existing Git repository with the following command:

```
cd existing_repo
git remote add origin https://github.com/warptr/docker.git
git branch -M main
git push -uf origin main
```

## Integrate with your tools

* [Set up project integrations](https://github.com/warptr/docker/settings)

## Collaborate with your team

* [Invite team members and collaborators](https://docs.github.com/en/account-and-profile/setting-up-and-managing-your-personal-account-on-github/managing-access-to-your-personal-repositories)
* [Create a new pull request](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/proposing-changes-to-your-work-with-pull-requests/creating-a-pull-request)
* [Automatically close issues from pull requests](https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue)
* [Enable merge request approvals](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/configuring-pull-request-approvals)

## Test and Deploy

Use the built-in continuous integration in GitHub.

* [Get started with GitHub Actions](https://docs.github.com/en/actions/quickstart)
* [Analyze your code for known vulnerabilities with CodeQL](https://docs.github.com/en/code-security/code-scanning/automatically-scanning-your-code-for-vulnerabilities-and-errors/automatically-scanning-your-code-for-vulnerabilities-and-errors-using-codeql)
* [Deploy to Kubernetes, Amazon EC2, or Amazon ECS](https://docs.github.com/en/actions/deployment/deploying-to-your-cloud-provider)
