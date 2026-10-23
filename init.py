   
import argostranslate.package
argostranslate.package.update_package_index()
packages = argostranslate.package.get_available_packages()
package = next(p for p in packages if p.from_code == "en" and p.to_code == "hi")
download_path = package.download()
argostranslate.package.install_from_path(download_path)